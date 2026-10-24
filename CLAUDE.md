# CLAUDE.md – automatic-gas-manipulation

## Idioma y audiencia
- Responder en **español**. La documentación (README, plc/README.md) y los mensajes del programa de Python se escriben en español sencillo: se los enseñan a los **electricistas de la planta**.
- Identificadores y comentarios del código en inglés.

## Seguridad (línea de gas)
- **Nunca mover la válvula desde Claude** (escribir %MW0/%MW2 o correr `valve_control.py` contra el PLC real) sin que el usuario lo pida explícitamente. Leer registros sí está bien.
- La seguridad vive en el PLC (watchdog, cierre al arrancar, límites, fallback), nunca solo en la PC/SCADA.
- El actuador S4 **no es a prueba de explosión** y sin la opción `-BSRC` se queda donde está al perder energía. La válvula de control no sustituye a las válvulas de corte de seguridad del quemador.

## Objetivo
- **Hoy:** controlar una válvula de bola con actuador eléctrico 4–20 mA desde un TM221, con posición real (feedback) y alarmas, y operarla/verla desde la PC.
- **Siguiente:** lazo cerrado de temperatura: TP100 (4–20 mA) → PID en el PLC → válvula de gas.
- **Proyecto final:** sistema de medición y control de insumos y parámetros de producción de una planta de globos de látex (~100 personas, 5 máquinas). Prioridad 1: **gas** de los quemadores. Luego agua, aceite, energía por máquina, temperatura de tinas de látex y coagulante, humedad relativa, velocidad del aire, entalpía (calculada) y motor principal. Empezar con **una máquina** y replicar.
- Arquitectura acordada (sin licencias): PLC (control y seguridad) → colector en Python (único que escribe al PLC) → base de datos de series de tiempo → Grafana para 3 usuarios (producción, ingeniería, dueño; usuarios los maneja TI) + página de control aparte.

## Hardware
| Equipo | Detalle |
|---|---|
| PLC | Schneider **TM221ME16R/G**, firmware **1.12.2.0** → el proyecto debe estar en **nivel funcional 12.0** (Programación → Comportamiento) |
| Salida analógica | **TM3AQ2/G** (módulo 1). Canal 0 = **%QW1.0**, bornes **Q0+ / Q0–** |
| Entradas analógicas | AI0 (%IW0.0) y AI1 (%IW0.1) integradas, **solo 0–10 V**, 10 bits (0–1000). 4–20 mA requiere **499 Ω 0.1–1 %, ½ W** entre AI+ y AI– (4 mA = 200, 20 mA = 1000). Para crecer: **TM3AI4** (4–20 mA directo, 12 bits) |
| Actuador | Assured Automation **S4**, posicionador DPS (se asume `-EP420L`): 4 mA = cerrada, 20 mA = abierta, carrera 11–40 s. DIN señal: pin 1 común –, pin 2 entrada +, pin 3 feedback + (salida **activa**) |
| Válvula | Assured Automation **Serie 26**, bola de acero inoxidable de paso completo (no lineal para control de flujo; V-port sería mejor) |
| Sensor de temperatura | PT100 de 3 hilos con **transmisor en cabeza 0–150 °C, 4–20 mA a 2 hilos, 12–35 VDC** (bornes 1 = +24V, 2 = –; PT100 en 3-4-5). Es **pasivo**: necesita +24 V en el lazo |
| Fuente | 24 VDC, 2 salidas + |

Hojas de datos en la raíz: `Datasheet.pdf` (actuador S4, cableado en la pág. 3) y `stainless steel 2 way ball valves.pdf`.

## Red
- **IPs fijas reservadas por TI (oct. 2026):** .22, .23, .24, .34, .37 (192.168.1.x / 255.255.255.0, gateway 192.168.1.101). Pedir a TI antes de usar cualquier otra.
  | IP | Equipo |
  |---|---|
  | 192.168.1.22 | PLC TM221 máquina 1 (antes .210; cambiar en ETH1 y descargar) |
  | 192.168.1.23 | PC del usuario = servidor del panel web (`http://192.168.1.23:8080`) |
  | .24 / .34 / .37 | Libres para los siguientes PLC (para 5 máquinas faltará al menos 1 más) |
- PLC: MAC `00-00-54-05-9f-3b` (Schneider = `00-00-54` / `00-80-F4`). Servidor Modbus TCP puerto 502, unit id 1. Programming protocol y Modbus server habilitados en ETH1.
- La red es la de oficina (impresoras, cámaras). **192.168.1.50 (impresora Lexmark) y .120 ya estaban ocupadas** y causaron "Duplicate IP". Antes de asignar IP: ping + `arp -a` para ver el fabricante por la MAC. A futuro: VLAN de control.
- El cable de red va en **ETH1**, no en **SL1** (serial RS-485, mismo conector RJ45).

## Mapa de registros (contrato PLC ↔ Python)
| Registro | Escribe | Significado |
|---|---|---|
| %MW0 | PC | Consigna 0…10000 = 0–100 % = 4–20 mA |
| %MW1 | PC | Heartbeat (cada 0.5 s) |
| %MW2 | PC | Habilitación (1 = la PC controla) |
| %MW3 | PLC | Último heartbeat visto |
| %MW10 | PLC | Salida enviada a %QW1.0 |
| %MW11 / %MW12 | PLC | Feedback crudo (%IW0.0) / posición 0…10000 |
| %MW13 | PLC | Bits: X0 watchdog, X1 error de posición, X2 falla feedback, X3 en posición, X4 FC cerrada (%I0.0), X5 FC abierta (%I0.1) |
| %MW14 | PLC | Desviación consigna – posición |
| %MW20 / %MW21 | PLC | Temperatura TP100 en décimas de °C / crudo %IW0.1 (rungs 24–26 de `plc/README.md`; **aún no cargados en el PLC**). Crudo < 180 = sin señal → el panel muestra "Sin señal" |

## Configuración del PLC (Machine Expert Basic)
- %QW1.0: tipo **4–20 mA**, mínimo **0**, máximo **10000**, fallback **0** (el rango "Normal" por defecto es 4000–20000; se editó a 0–10000).
- %IW0.0 / %IW0.1: 0–10 V, Normal 0–1000.
- Timers: %TM0 TON 100 ms ×20 (watchdog 2 s), %TM1 ×600 (60 s posición), %TM2 ×10 (1 s falla feedback).
- Comportamiento: fallback values; modo de arranque debe ser **Start In Run** (el rung de arranque deja la válvula cerrada).
- Programa: rung de arranque + rungs 0–23 (válvula) cargados; rungs 24–26 (temperatura) documentados pero pendientes (ver `plc/README.md`). Rung 0 = arranque (`LD SB_FIRSTRUN` = %S13 → %MW0 := 0, %MW2 := 0). `/%M100` = "siempre verdadero".
- El usuario prefiere ver el programa en **ladder** pero escribe los rungs en **IL** y convierte con "> LD". Reglas IL del TM221: **una sola `LD` por rung**; timers con `BLK %TMx … IN … OUT_BLK … LD Q … END_BLK`; operaciones con varias líneas `[..]` tras un `LD`.

## Problemas ya resueltos (para diagnosticar rápido)
| Síntoma | Causa |
|---|---|
| "Powerless" / IP 0.0.0.0 | PLC solo alimentado por USB; falta 24 V |
| No descarga: niveles funcionales incompatibles | Proyecto en 14.0 y firmware solo acepta 12.0 |
| Modo BOOT, LED SD parpadeando, ERR rojo | **Tarjeta SD** dentro del PLC; sacarla con todo apagado |
| Ethernet status 1 "Duplicate IP" / ERR parpadeando | IP ya usada por otro equipo |
| Ethernet status 4 / IP 10.10.x.x | Configuración ETH1 no descargada o inválida |
| Watchdog siempre activo | Faltaba el rung `LD [%MW1 <> %MW3] / ST %M0`, o el PLC en STOP |
| %MW3 no sigue a %MW1 | PLC en STOP (LED RUN parpadeando) |
| Orden correcta pero la válvula no se mueve | %QW1.0 estaba en "Not used" |
| Al pasar a RUN la válvula no responde | El rung de arranque pone %MW2 = 0: reiniciar Python |
| LED BAT rojo | Pila del reloj; no afecta |

## Archivos
- `pc/valve_control.py`: cliente Modbus TCP propio (sin dependencias), modos `interactive` y `sweep`. Ej.: `python pc/valve_control.py --ip 192.168.1.22 interactive`.
- `pc/valve_web.py`: panel web (stdlib, sin dependencias) que reutiliza `ModbusTcp`/`Valve` de `valve_control.py`; sirve `dashboard/valvula.html` y una API JSON (`/api/status`, `/api/history?min=N` [t, orden, posición, temp] con buffer de 1 h, `/api/history.csv`, POST `/api/setpoint`, `/api/enable`, `/api/close`). Reconecta solo. Todo requiere login (cookie de sesión HttpOnly/SameSite=Strict, POST solo con JSON); rol `operador` escribe, `lector` solo ve; 5 fallos por IP → bloqueo 5 min; HTTPS con `--cert/--key`. Ej.: `python pc/valve_web.py --plc 192.168.1.22` → `http://<IP PC>:8080`. Páginas: `dashboard/valvula.html`, `dashboard/login.html`.
- Pantallas de `dashboard/valvula.html` (rutas con #): `#/` Inicio (botón MB24) → `#/mb24` "Centro de visualización y control de datos" (botón Quemadores de gas) → `#/mb24/quemadores` (temperatura + válvula con ▲▼ de 5 %, Enviar, Visualizar tendencia, Descargar CSV, Tomar control/Liberar/Cerrar). Barra superior siempre con Inicio, PLC+IP, usuario. Las ▲▼ de temperatura están deshabilitadas hasta que exista el PID. Logo en `dashboard/imagenes/` servido en `/imagenes/...` (público). El panel toma el puerto en exclusiva: un segundo panel no arranca.
- `pc/usuarios.py`: cuentas (PBKDF2-SHA256) en `pc/usuarios.json` (en `.gitignore`, igual que `*.pem`). `agregar <n> --rol operador|lector`, `contrasena`, `rol`, `quitar`, `lista`.
- `plc/README.md`: configuración, mapa de memoria y los rungs (ladder + IL).
- `README.md`: cableado, instalación, seguridad y uso (para electricistas).
- `dashboard/maqueta.html`: maqueta del tablero del lazo de temperatura con datos simulados (sin conexión al PLC).
- `compras/cotizacion_fase1.xlsx`: lista de cotización de la fase 1 (gas máquina 1 + quemador piloto), con precios de referencia de EE. UU. (oct. 2026), búsquedas en Amazon y estimado puesto en Guatemala (supuestos editables en la hoja "Supuestos").

## Datos de la planta (Guatemala)
- Gas **LP**; por máquina entra a 40 psi → regulador a 20 psi → manómetro → solenoide de seguridad → válvula manual (a cambiar por la modulante) → quemador. Línea de 1/2".
- **5 máquinas, 4 quemadores por máquina**, cada uno con su zona y temperatura independiente → 4 lazos PID por máquina.
- Máquinas "artesanales" con algunos **Siemens LOGO!** que se quieren pasar a PLC. Marca de PLC para la planta: por decidir (Schneider vs Siemens S7-1200) según cotizaciones locales.
- Pendiente de confirmar: detección de llama actual de los quemadores, consumo de gas (BTU/h o gal/mes), tamaño del tanque.
- Para probar el cliente sin PLC se usó un servidor Modbus falso en el scratchpad; no está en el repo.
