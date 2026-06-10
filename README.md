# CyberShop Desktop Offline

App de escritorio nativa (PyQt6) que funciona **sin internet** y replica los
colores de marca y el lector de codigo de barras del POS web.

## Requisitos

- Windows 10/11
- Python 3.11+ en el PATH (`python --version` debe responder)

El primer arranque crea automaticamente un `venv\` local e instala PyQt6.

## Ejecutar en local (sin compilar)

Doble clic en `run.bat` o desde una terminal en `C:\Cybershop\CyberShopDesktop`:

```bat
run.bat
```

Tambien funciona el comando directo (cuando ya existe el venv):

```bat
venv\Scripts\python.exe main.py
```

## Login inicial

```text
admin@cybershop.local
admin123
```

Este usuario local trae `must_change_password=1`: la primera vez obliga a
definir una contraseña nueva (mínimo 6 caracteres).

### Login offline vs. remoto (mismo usuario que la web)

`LoginView._login` (`main.py`) intenta primero **login remoto** si hay sync
configurado (`base_url` + `api_key` en `sync_config.json`):

1. **Remoto** — `POST /api/v1/sync/auth` valida email/contraseña contra la tabla
   `usuarios` del tenant en el servidor. Si el servidor confirma,
   `LocalStore.cache_remote_login()` crea/actualiza el usuario local y **guarda
   el hash PBKDF2 de la contraseña recién verificada**. Si el servidor responde
   401/403 (credenciales malas o cuenta inhabilitada) **no** cae a local.
2. **Offline** — si no hay sync configurado o no hay red, valida contra el hash
   local en la tabla `users` (`LocalStore.authenticate()`).

Consecuencia: un usuario de la web entra al escritorio con **el mismo correo y
contraseña**; tras el primer login con internet, ese usuario también funciona
**sin internet**. Hashes con iteraciones antiguas se re-hashean al vuelo.
Detalle de la cadena: [../CyberShop/app/docs/INTEGRACION_WEB_DESKTOP.md](../CyberShop/app/docs/INTEGRACION_WEB_DESKTOP.md).

## Almacenamiento local

Tanto la base SQLite como la marca del cliente se guardan en:

```text
%APPDATA%\CyberShopNative\
   cybershop_offline.db   <- datos (productos, ventas, usuarios)
   branding.json          <- colores, logo y datos de empresa
```

Si esa ruta no es escribible, se usa como fallback
`CyberShopDesktop\desktop_native_data\` (junto al ejecutable).

Para reiniciar datos: cerrar la app y borrar el archivo correspondiente.

### Esquema de la base SQLite (`cybershop_offline.db`)

Definido en `local_store.py::_init_schema()` (migraciones idempotentes con
`_ensure_column`). 16 tablas:

| Tabla | Propósito |
|---|---|
| `users` | Usuarios locales. Hash PBKDF2-SHA256 (600k iter.). `remote_id` enlaza al usuario web; `must_change_password` fuerza cambio en primer login |
| `products` | Catálogo local. `sku` UNIQUE, índice por `barcode`; `genero_id`/`remote_id` enlazan a la web (match por SKU) |
| `generos` | Categorías de producto. UPSERT por `remote_id` |
| `sales` | Ventas POS locales. `receipt_number` UNIQUE (`LOCAL-NNNN`) |
| `sale_items` | Renglones de cada venta (FK a `sales`/`products`) |
| `inventory_movements` | Log de ajustes de stock (delta + motivo) |
| `outbox` | Cola de cambios locales pendientes de empujar al servidor (`entity`, `entity_id`, `action`, `payload` JSON, `synced_at`) |
| `metadata` | Clave-valor (flags como `seed_done`) |
| `remote_sales_cache` | Caché de ventas web (`/sync/sales_web`) — solo lectura para mostrar |
| `remote_inventory_cache` | Caché de inventario web (`/sync/inventory_log`) — solo lectura |
| `rt_tables` / `rt_orders` / `rt_consumptions` | Espejo local del módulo Restaurante (mesas, cuentas abiertas, consumos). Flag `synced`: 0 = cambio local pendiente (el pull lo preserva), 1 = verdad del servidor |
| `cb_movimientos` / `cb_plantillas` / `cb_cierres` | Espejo local de Contabilidad (movimientos con impuestos, plantillas recurrentes, cierres de período). Mismo patrón `synced` |

Borrados son **soft delete** (`active=0`) para preservar historial de sync.

## Lector de codigo de barras

Replica el `ScannerEngine` del POS web (`facturacion_pos.html`).

- Funciona con cualquier pistola USB que emule teclado.
- Tambien acepta entrada manual: escribir el codigo y `Enter` en el campo verde.
- Detecta entrada rapida (<= 50 ms entre teclas) aunque el foco no este en el
  campo del POS, y la enruta al carrito automaticamente.
- Busca por `barcode` y, como fallback, por `sku`.
- Suma 1 unidad si el producto ya esta en el carrito.
- Beep del sistema y aviso visual si el codigo no existe o no hay stock.
- Tres productos demo vienen con codigos `7701234567890..892`.

El escaner se activa automaticamente al abrir la pagina **POS** y se
desactiva al salir, para no interferir con la edicion de productos o usuarios.

## Personalizacion para nuevos clientes

Toda la marca (colores, logo, nombre, datos) se controla desde la pagina
**Configuracion (F8)** dentro de la app o editando directamente el archivo:

```text
%APPDATA%\CyberShopNative\branding.json
```

### Desde la UI (recomendado)

1. Login y abrir **Configuracion** (sidebar o `F8`).
2. Cambiar nombre, slogan, telefono, direccion, web, pie de recibo.
3. Subir logo con **Examinar...** (PNG/JPG, se reescala a 56-72 px de alto).
4. Cambiar colores con el selector visual o pegando un hex `#RRGGBB`.
5. **Guardar y aplicar** persiste a `branding.json` y refresca toda la UI sin
   reiniciar.
6. **Previsualizar** aplica sin guardar (vuelve a defaults al cerrar la app).
7. **Restaurar predeterminados** borra `branding.json` y vuelve a la marca
   CyberShop.

### Clonar la marca a otra instalacion

En el cliente origen: **Exportar JSON...** → guarda `branding.json` donde
quieras. En la maquina nueva: **Importar JSON...** → reemplaza la marca
actual y se aplica al instante.

Tambien funciona copiar el archivo a mano:

```text
copy branding.json C:\<otra-pc>\AppData\Roaming\CyberShopNative\branding.json
```

### Claves disponibles en branding.json

```json
{
  "empresa": {
    "nombre": "CyberShop",
    "slogan": "Panel administrativo local sin internet",
    "ventana_titulo": "CyberShop Desktop Offline",
    "email": "",
    "telefono": "",
    "direccion": "",
    "website": "",
    "logo_path": "",
    "recibo_pie": "Gracias por su compra."
  },
  "colores": {
    "primario": "#122C94",
    "primario_oscuro": "#091C5A",
    "acento": "#fb8500",
    "acento_secundario": "#a6c438",
    "peligro": "#b42318",
    "sidebar_inicio": "#091C5A",
    "sidebar_fin": "#122C94",
    "fondo": "#f8faff"
  }
}
```

| Color               | Donde se ve                                        |
|---------------------|----------------------------------------------------|
| `primario`          | Botones, focus, links, headers de tablas           |
| `primario_oscuro`   | Titulos de pagina, hover de botones, sidebar fin   |
| `acento`            | Marcador del item activo del sidebar, focus barcode|
| `acento_secundario` | Lector de barras, boton "Finalizar venta"          |
| `peligro`           | Errores, badge de scanner inactivo, botones rojos  |
| `sidebar_inicio`    | Color superior del gradiente del sidebar           |
| `sidebar_fin`       | Color inferior del gradiente del sidebar           |
| `fondo`             | Fondo general de la app y del login                |

> Los datos vacios en `empresa` simplemente se ocultan (por ejemplo, si no hay
> direccion no se imprime esa linea en el recibo).

## Generar EXE distribuible

```bat
build_exe.bat
```

Salida: `dist\CyberShopOffline\CyberShopOffline.exe`

El `.exe` no requiere Python instalado en la maquina destino. Copia toda la
carpeta `dist\CyberShopOffline\` para distribuir. Los archivos `branding.json`
y `cybershop_offline.db` se crean en `%APPDATA%\CyberShopNative\` la primera
vez que se ejecute la app en la maquina destino.

## Generar instalador con asistente

Para distribuir a clientes finales, en vez de copiar `dist\` a mano, usa el
instalador real con asistente. Requiere [Inno Setup 6](https://jrsoftware.org/isdl.php)
instalado en el PATH default (`C:\Program Files (x86)\Inno Setup 6\`).

```bat
build_installer.bat
```

Esto:
1. Corre `build_exe.bat` (PyInstaller).
2. Compila `installer.iss` con Inno Setup → produce `Output\CyberShopSetup.exe`.
3. Copia el resultado a `..\CyberShop\app\static\installers\CyberShopSetup_base.exe`,
   que es el binario que sirve `/descargar` del portal.

### Flujo de distribución

1. **Admin** corre `python tools/crear_sync_key.py --tenant-slug <slug> --label <etiqueta>`
   en el servidor → obtiene un `client_code` corto (ej. `CYB-A3F2K9P1`) y la `api_key`.
2. **Admin** entrega al cliente solo el `client_code`.
3. **Cliente** abre `https://<server>/descargar`, pega su código y baja un ZIP
   con `CyberShopSetup.exe` + `bootstrap.json` (preconfigurado para su tenant).
4. **Cliente** extrae el ZIP, ejecuta `CyberShopSetup.exe`. El asistente lee
   `bootstrap.json` y pre-llena los campos URL/API key/slug.
5. Tras instalar, abre la app desde el menú inicio. La primera sincronización
   trae productos, branding (colores y logo del tenant) y datos de empresa.

## Sincronización de datos (pull / push / outbox)

Disparada manualmente desde **Sincronización (F7)** o por timer de fondo
(`enabled` + `interval_sec` en `sync_config.json`, default 30 s). Cliente HTTP
sin dependencias externas: `sync_client.py` (solo `urllib`).

**Pull (servidor → escritorio), incremental por cursor:**

- `/api/v1/sync/products?since=<cursor>` → `upsert_product_from_remote()` (match por SKU)
- `/api/v1/sync/users?since=<cursor>` → `upsert_user_from_remote()` (match por email)
- `/api/v1/sync/generos?since=<cursor>` → `upsert_genero_from_remote()` (match por `remote_id`)
- `/api/v1/sync/sales_web` y `/inventory_log` → cachés de solo lectura

Cada entidad guarda su propio cursor (`cursor_products`, `cursor_users`, …) en
`sync_config.json`; se actualizan `last_sync_at`/`last_sync_status`.

**Push (escritorio → servidor):**

1. `LocalStore.pending_outbox()` toma items con `synced_at IS NULL`.
2. `POST /api/v1/sync/outbox` con `{items:[{entity,entity_id,action,payload}]}`
   (entidades: sale, inventory_movement, product, user, category, order).
3. Éxito → `mark_outbox_synced(ids)`. Fallo → quedan en cola para el próximo intento.

Resolución de conflictos: **LWW (last-write-wins)** — match por SKU/email,
el servidor decide con `updated_at`.

## Sincronización de branding (colores/logo) desde el servidor

Al arrancar y tras login exitoso, la app llama `/api/v1/sync/branding` y
aplica los colores/logo configurados en `/admin/configuracion-cliente` del
servidor. Mapeo de claves:

| Web (`cliente_config.colores.*`) | Desktop (`branding.json.colores.*`) |
|---|---|
| `primario`, `primario_oscuro`, `acento`, `acento_secundario` | (mismo nombre) |
| `secundario` | `sidebar_inicio` |
| `botones` | `sidebar_fin` |

Si se edita el branding desde F8 y se quiere que el servidor no lo pise,
activar `branding_local_override: true` en `sync_config.json`.

## Auto-update

Tras login, la app llama `/api/v1/sync/version`. Si la versión publicada en
`server/static/installers/version.json` es mayor que `APP_VERSION` (constante
en `main.py`), se muestra un diálogo con tres opciones:

- **Descargar e instalar** → baja el .exe del servidor y lo lanza (cierra la app).
- **Más tarde** → vuelve a notificar al próximo arranque.
- **Saltar esta versión** → marca `skip_version` en `sync_config.json` y no
  vuelve a notificar hasta que salga una versión mayor.

Para deshabilitar el auto-update en una instalación, poner `AUTO_UPDATE_CHECK=false`
en `%APPDATA%\CyberShopNative\.cybershop.conf`.

## Estructura de modulos

| Atajo | Modulo         | Descripcion                                       |
|-------|----------------|---------------------------------------------------|
| F1    | Dashboard      | Metricas: productos, stock bajo, ventas hoy, sync |
| F2    | Productos      | CRUD local con SKU + codigo de barras + buscador  |
| F3    | POS            | Lector de barras + venta directa + control stock  |
| F4    | Inventario     | Movimientos de entrada/salida con motivo          |
| F5    | Ventas         | Historial + recibos + total dia/mes/historico     |
| F6    | Usuarios       | CRUD local con roles y cambio de contrasena       |
| F7    | Sincronizacion | Estado de la BD y outbox local                    |
| F8    | Configuracion  | Branding (colores, logo, datos de empresa)        |
| F9    | Restaurante    | Atencion de mesas offline-first: abrir mesa, consumos con avance pendiente→preparando→servido, vista cajero resumida, cobrar/cerrar (crea movimiento contable en el servidor) |
| F10   | Contabilidad   | Dashboard, movimientos (con impuestos retefuente/IVA/reteIVA/reteICA), plantillas recurrentes, cierres de período y export CSV |

> La visibilidad de cada módulo depende del **rol** del usuario (ver "Roles y permisos").

## Roles y permisos (RBAC)

El escritorio replica los roles de la web (tabla `roles` del tenant). Al hacer
login (remoto o offline), `map_role(rol_id)` resuelve el rol y `ROLE_MODULES`
define qué módulos ve cada uno (`main.py`):

| Rol (web → escritorio) | Módulos visibles |
|---|---|
| 1 admin / 2 propietario → **Administrador** | Todos (F1–F10) |
| 4 → **Empleado** | Dashboard, POS, Restaurante, Ventas, Productos, Inventario |
| 7 → **Cajero** | Dashboard, POS, Restaurante, Ventas (vista cajero resumida en Restaurante; puede cancelar pedidos) |
| 6 → **Mesero** | Dashboard, POS, Restaurante (no puede cancelar pedidos) |
| 5 → **Contador** | Dashboard, Ventas, Contabilidad |
| 3 → **Cliente** | Solo Dashboard (no debería usar el escritorio) |

La navegación oculta los módulos no permitidos y `_show_section` bloquea el
acceso directo. Espejo de los grupos de `security.py` de la web
(`ADMIN_FULL`, `POS_OPERATIONAL`, `RESTAURANT_*`, `ADMIN_CONTADOR`).

## Mapa de archivos

| Archivo | Para qué sirve |
|---|---|
| `main.py` (~5700 líneas) | App PyQt6. Clases clave: `DesktopShell` (ventana + sidebar + atajos F1–F8), `LoginView`/`ChangePasswordDialog` (auth), `ScannerEngine` (lector de barras, <50 ms), y una vista por módulo: `DashboardPage`, `ProductsPage`, `PosPage`, `InventoryPage`, `SalesPage`, `UsersPage`, `SyncPage`, `ConfiguracionPage`, `RestaurantPage` (F9), `ContabilidadPage` (F10); RBAC vía `map_role` + `ROLE_MODULES` |
| `local_store.py` | Capa SQLite: esquema, CRUD productos/usuarios/ventas, hashing PBKDF2, outbox, cachés remotas, `cache_remote_login`, `upsert_*_from_remote` |
| `sync_client.py` | Cliente HTTP de `/api/v1/sync/*` (solo `urllib`, sin dependencias) |
| `sync_config.py` | Estado de sync persistente (`sync_config.json`: base_url, api_key, cursores, last_sync) |
| `cybershop_conf.py` | Lee/escribe `.cybershop.conf` (SERVER_URL, SYNC_API_KEY, TENANT_*) — lo crea el instalador |
| `branding.py` | Marca: carga/guarda `branding.json`, valida hex, renderiza QSS, aplica branding remoto |
| `run.bat` | Lanzador dev: crea venv, instala PyQt6, corre `main.py` |
| `build_exe.bat` | PyInstaller → `dist\CyberShopOffline\CyberShopOffline.exe` |
| `build_installer.bat` | Pipeline completo: PyInstaller + Inno Setup → `CyberShopSetup_base.exe` al servidor |
| `installer.iss` | Script Inno Setup 6 (asistente, lee `bootstrap.json`, escribe `.cybershop.conf`) |
| `CyberShopOffline.spec` | Spec PyInstaller activo (entry: `main.py`) |
| `CyberShopDesktop.spec` | Spec legacy (entry inexistente `desktop\cybershop_desktop.py`) — **no usar** |
| `requirements.txt` | Única dependencia: `PyQt6>=6.5` |
| `assets/cybershop.ico` / `.png` | Icono de ventana / fallback embebido en el bundle |

> Mapa del proyecto completo (web + escritorio): [../CyberShop/app/docs/MAPA_ARCHIVOS.md](../CyberShop/app/docs/MAPA_ARCHIVOS.md).
