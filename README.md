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
