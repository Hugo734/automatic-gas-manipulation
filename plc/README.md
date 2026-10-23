# Programa PLC – TM221ME16R + TM3AQ2 (EcoStruxure Machine Expert – Basic)

## Cómo funciona el control de posición

```
 PC / SCADA          PLC TM221                  TM3AQ2            Actuador S4 (posicionador DPS)
 consigna %  -->  %MW0 -> límites -> %QW1.0 --> 4-20 mA  -->  el posicionador mueve el motor
                                                               hasta que la posición = consigna
                  %IW0.0 <-- 500 Ω <------------ 4-20 mA  <--  salida de posición real (pin 3)
                     |
                     +--> compara consigna vs. posición real --> alarmas
```

- **El lazo cerrado de posición lo hace el posicionador DPS dentro del actuador.**
  El PLC solo le da la consigna (4–20 mA), y el posicionador mueve el motor hasta que la posición real es igual a la consigna.
  Por eso el PLC **no necesita un PID** para posicionar la válvula.
- **El PLC verifica** con el feedback que la válvula realmente llegó.
  Genera alarmas si no llega, si la señal de feedback se pierde, o si la PC deja de comunicarse.
- El programa de Python hace de **simulador de la consigna**: le dice al PLC cuántos mA mandar y muestra la posición real que regresa.

## 1. Configuración del hardware

### Controlador TM221ME16R – ETH1
| Parámetro | Valor |
|---|---|
| Dirección IP | Fija, `192.168.1.22` / `255.255.255.0`, gateway `192.168.1.101` |
| Servidor Modbus | **Habilitado**, puerto 502 |

### Entrada analógica integrada %IW0.0 (feedback de posición)
| Parámetro | Valor |
|---|---|
| Tipo | 0 – 10 V |
| Rango (Scope) | Normal → 0 … 1000 |
| Filtro | 1 (subir si la lectura tiene ruido) |

Las entradas integradas del TM221 solo miden voltaje.
El feedback de 4–20 mA se lee con una **resistencia de 499 Ω (0.1 %, ½ W)** entre IN0 y COM:

| Corriente | Voltaje | Cuentas |
|---|---|---|
| 4 mA | 2 V | 200 |
| 20 mA | 10 V | 1000 |

La resolución es de 800 pasos (≈ 0.13 %).
Si se necesita más precisión, se puede agregar un módulo **TM3AI2** (4–20 mA directo, 12 bits, sin resistencia).

### Módulo de expansión TM3AQ2 (módulo 1)
| Parámetro | %QW1.0 (canal 0) |
|---|---|
| Tipo | **4 – 20 mA** |
| Rango (Scope) | **Normal** → 0 … 10000 (0 = 4 mA, 10000 = 20 mA) |
| Valor de repliegue (fallback) | **0** (4 mA = válvula cerrada si el PLC pasa a STOP) |

En *Configuración → Comportamiento / Fallback* elegir **valores de repliegue** (no "mantener valores").

### Temporizadores
| Timer | Tipo | Base de tiempo | Preset | Uso |
|---|---|---|---|---|
| %TM0 | TON | 100 ms | 20 (2 s) | Watchdog de comunicación con la PC |
| %TM1 | TON | 100 ms | 600 (60 s) | Tiempo máximo para llegar a posición (debe ser mayor que la carrera completa del actuador, 11–40 s) |
| %TM2 | TON | 100 ms | 10 (1 s) | Retardo de falla de señal de feedback |

## 2. Mapa de memoria (registro Modbus n = %MWn)

| Registro | Lo escribe | Significado |
|---|---|---|
| %MW0 | PC | Consigna 0 … 10000 = 0.00 … 100.00 % abierta = 4.00 … 20.00 mA |
| %MW1 | PC | Contador de vida (heartbeat), cambia cada 0.5 s |
| %MW2 | PC | Habilitación: 1 = la PC controla la válvula, otro valor = cerrada |
| %MW3 | PLC | Último heartbeat visto (interno) |
| %MW10 | PLC | Valor enviado a %QW1.0 (consigna real) |
| %MW11 | PLC | Feedback crudo (%IW0.0, 0 … 1000) |
| %MW12 | PLC | Posición real 0 … 10000 (0.00 … 100.00 %) |
| %MW13 | PLC | Palabra de estado (ver bits abajo) |
| %MW14 | PLC | Desviación = consigna − posición real (con signo) |

| Bit de %MW13 | Significado |
|---|---|
| X0 | Alarma watchdog (la PC dejó de comunicarse) |
| X1 | Alarma error de posición (no llegó en el tiempo de %TM1) |
| X2 | Alarma falla de señal de feedback (cable cortado / < 3.6 mA) |
| X3 | En posición (desviación dentro de ±2 %) |
| X4 | Final de carrera CERRADA (%I0.0) |
| X5 | Final de carrera ABIERTA (%I0.1) |

| Bit | Significado |
|---|---|
| %M0 | El heartbeat cambió en este ciclo |
| %M1 | Watchdog disparado |
| %M2 | Estado seguro (watchdog o no habilitado) → salida 4 mA |
| %M3 | Fuera de tolerancia (desviación > ±2 %) |
| %M4 | Error de posición |
| %M5 | En posición |
| %M6 | Falla de señal de feedback |
| %M100 | **No se usa nunca.** El contacto cerrado `/%M100` = "siempre VERDADERO" |

## 3. Escalones (rungs) en Ladder

`[ ... ]` con comparación = **Bloque de comparación**; `[ ... := ... ]` = **Bloque de operación**.

### A. Comunicación con la PC (watchdog)
```
Rung de arranque (va ANTES del rung 0) – al pasar a RUN: consigna 0 y deshabilitado
|--| %S13 |-------------------------------+--[ %MW0 := 0 ]--------|
                                          +--[ %MW2 := 0 ]--------|

Rung 0 – detectar cambio del heartbeat
|--[ %MW1 <> %MW3 ]--------------------------------------( %M0 )--|

Rung 1 – guardar el último heartbeat
|--| %M0 |-----------------------------------[ %MW3 := %MW1 ]-----|

Rung 2 – watchdog: cuenta mientras el heartbeat NO cambia
|--|/ %M0 |------------[ %TM0  TON 2 s ]-------------------( %M1 )--|
                         IN          Q

Rung 3 – estado seguro
|--+--| %M1 |-------+------------------------------------( %M2 )--|
   |                |
   +--[ %MW2 <> 1 ]-+
```

### B. Consigna hacia la válvula (4–20 mA)
```
Rung 4 – normal: tomar la consigna de la PC
|--|/ %M2 |----------------------------------[ %MW10 := %MW0 ]----|

Rung 5 – seguro: 4 mA (cerrada)
|--| %M2 |-----------------------------------[ %MW10 := 0 ]-------|

Rung 6 – límite alto
|--[ %MW10 > 10000 ]-------------------------[ %MW10 := 10000 ]---|

Rung 7 – límite bajo
|--[ %MW10 < 0 ]-----------------------------[ %MW10 := 0 ]-------|

Rung 8 – escribir la salida analógica
|--|/ %M100 |--------------------------------[ %QW1.0 := %MW10 ]--|
```

### C. Feedback de posición
```
Rung 9 – feedback crudo
|--|/ %M100 |--------------------------------[ %MW11 := %IW0.0 ]--|

Rung 10 – escalar: (crudo - 200) * 12.5  -> 0..10000
|--|/ %M100 |------------------[ %MW12 := (%MW11 - 200) * 25 / 2 ]-|

Rung 11 – límite bajo
|--[ %MW12 < 0 ]-----------------------------[ %MW12 := 0 ]-------|

Rung 12 – límite alto
|--[ %MW12 > 10000 ]-------------------------[ %MW12 := 10000 ]---|
```

### D. Verificación de posición y alarmas
```
Rung 13 – desviación = consigna - posición real
|--|/ %M100 |--------------------------[ %MW14 := %MW10 - %MW12 ]-|

Rung 14 – fuera de tolerancia (±2 % = ±200)
|--+--[ %MW14 > 200 ]--+---------------------------------( %M3 )--|
   |                   |
   +--[ %MW14 < -200 ]-+

Rung 15 – error de posición: fuera de tolerancia por más de 60 s (solo si el feedback está bien)
|--| %M3 |----|/ %M6 |----[ %TM1  TON 60 s ]--------------( %M4 )--|
                         IN          Q

Rung 16 – en posición (dentro de tolerancia y con feedback correcto)
|--|/ %M3 |----|/ %M6 |----------------------------------( %M5 )--|

Rung 17 – falla de señal feedback: menos de 3.6 mA (180) por 1 s
|--[ %MW11 < 180 ]-----[ %TM2  TON 1 s ]-------------------( %M6 )--|
                         IN          Q
```

### E. Palabra de estado para la PC
Cada uno es un rung separado: el TM221 solo acepta una condición `LD` por rung.
```
Rung 18 |--| %M1 |---------------------------------------( %MW13:X0 )--|
Rung 19 |--| %M4 |---------------------------------------( %MW13:X1 )--|
Rung 20 |--| %M6 |---------------------------------------( %MW13:X2 )--|
Rung 21 |--| %M5 |---------------------------------------( %MW13:X3 )--|
Rung 22 |--| %I0.0 |-------------------------------------( %MW13:X4 )--|
Rung 23 |--| %I0.1 |-------------------------------------( %MW13:X5 )--|
```

Notas:
- En el rung 10, el valor máximo es 800 × 25 = 20000, que cabe en un INT.
- La tolerancia de ±2 % (200) y los 60 s de %TM1 son ajustables. Primero hay que medir con el programa de Python cuánto tarda y qué tan exacta llega la válvula real, y después ajustarlos.
- La falla de feedback **no cierra la válvula**: el posicionador sigue funcionando por su cuenta. Solo da alarma.

## 4. Cómo dibujar los rungs en Machine Expert Basic

| Elemento | Barra de herramientas | Uso |
|---|---|---|
| Contacto normalmente abierto `-\| \|-` | Contactos | `%M0`, `%M1`, `%I0.0` … |
| Contacto normalmente cerrado `-\|/\|-` | Contactos | `/%M0`, `/%M2`, `/%M100` |
| Bobina `-( )-` | Bobinas | `%M0`, `%M1`, `%MW13:X0` … |
| Bloque de comparación `[<]` | Comparación | `%MW1<>%MW3`, `%MW10>10000` … (lado izquierdo) |
| Bloque de operación `[...]` | Operación | `%MW3:=%MW1` … (siempre lado derecho) |
| Temporizador | Bloques de función → Timer | `%TM0`, `%TM1`, `%TM2` |
| Lápiz (dibujar línea) | Líneas | Conectar elementos y ramas en paralelo (OR) |

Para cada rung:
1. Clic en **Insertar rung**.
2. Clic en el elemento de la barra y luego en la celda de la cuadrícula.
3. Doble clic en el elemento y escribir la dirección o expresión.
4. El rung deja de estar en rojo cuando está completo y correcto.

Rungs con temporizador (2, 15, 17):
1. Poner el contacto o la comparación.
2. Agregar **Bloques de función → Timer** y con doble clic elegir %TMx.
3. Conectar a **IN** y poner la bobina en **Q**.

Rungs con OR (3 y 14):
1. Poner el primer elemento y la bobina en la línea de arriba.
2. Poner el segundo elemento en la línea de abajo.
3. Con el **lápiz**, unir su extremo derecho con la línea de arriba.

**Alternativa:** cambiar el rung de **LD** a **IL**, escribir el texto (por ejemplo `LDN %M100` y `[%QW1.0 := %MW10]`), y presionar **> LD** para verlo en escalera.

## 4b. Texto IL de cada rung (para escribir y convertir con "> LD")

| Rung | IL |
|---|---|
| Arranque | `LD %S13` · `[%MW0 := 0]` · `[%MW2 := 0]` |
| 0 | `LD [%MW1 <> %MW3]` · `ST %M0` |
| 1 | `LD %M0` · `[%MW3 := %MW1]` |
| 2 | `BLK %TM0` · `LDN %M0` · `IN` · `OUT_BLK` · `LD Q` · `ST %M1` · `END_BLK` |
| 3 | `LD %M1` · `OR [%MW2 <> 1]` · `ST %M2` |
| 4 | `LDN %M2` · `[%MW10 := %MW0]` |
| 5 | `LD %M2` · `[%MW10 := 0]` |
| 6 | `LD [%MW10 > 10000]` · `[%MW10 := 10000]` |
| 7 | `LD [%MW10 < 0]` · `[%MW10 := 0]` |
| 8 | `LDN %M100` · `[%QW1.0 := %MW10]` |
| 9 | `LDN %M100` · `[%MW11 := %IW0.0]` |
| 10 | `LDN %M100` · `[%MW12 := %MW11 - 200]` · `[%MW12 := %MW12 * 25]` · `[%MW12 := %MW12 / 2]` |
| 11 | `LD [%MW12 < 0]` · `[%MW12 := 0]` |
| 12 | `LD [%MW12 > 10000]` · `[%MW12 := 10000]` |
| 13 | `LDN %M100` · `[%MW14 := %MW10 - %MW12]` |
| 14 | `LD [%MW14 > 200]` · `OR [%MW14 < -200]` · `ST %M3` |
| 15 | `BLK %TM1` · `LD %M3` · `ANDN %M6` · `IN` · `OUT_BLK` · `LD Q` · `ST %M4` · `END_BLK` |
| 16 | `LDN %M3` · `ANDN %M6` · `ST %M5` |
| 17 | `BLK %TM2` · `LD [%MW11 < 180]` · `IN` · `OUT_BLK` · `LD Q` · `ST %M6` · `END_BLK` |
| 18 | `LD %M1` · `ST %MW13:X0` |
| 19 | `LD %M4` · `ST %MW13:X1` |
| 20 | `LD %M6` · `ST %MW13:X2` |
| 21 | `LD %M5` · `ST %MW13:X3` |
| 22 | `LD %I0.0` · `ST %MW13:X4` |
| 23 | `LD %I0.1` · `ST %MW13:X5` |

(`·` = nueva línea)

## 5. Prueba sin la PC

1. Descargar el programa y poner en RUN.
2. En una **Tabla de animación**, agregar %MW0, %MW2, %MW10, %QW1.0, %MW11, %MW12, %MW13 y %MW14.
3. El watchdog se dispara porque nadie cambia %MW1.
   Para probar a mano, poner temporalmente el preset de %TM0 en 9999.
4. Escribir %MW2 = 1, y luego %MW0 = 0 / 2500 / 5000 / 7500 / 10000.
5. Verificar con el multímetro en serie: 4 / 8 / 12 / 16 / 20 mA.
6. Verificar que %MW12 (la posición real) sigue a %MW0, y que %MW13:X3 (en posición) se activa.
