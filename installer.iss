; ===============================================================
; CyberShop POS Desktop — Instalador con asistente (Inno Setup 6+)
;
; Compilar:    "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer.iss
; Salida:      Output\CyberShopSetup.exe
;
; Si bootstrap.json existe al lado del setup.exe (caso normal: el cliente
; lo descargó desde /descargar y extrajo el ZIP), el wizard pre-llena los
; campos automáticamente. Si no existe, los campos quedan vacíos y el
; cliente los completa a mano.
;
; El asistente escribe `%APPDATA%\CyberShopNative\.cybershop.conf` con
; los valores capturados — la app desktop lo lee al arrancar (cybershop_conf.py).
; ===============================================================

#define AppName       "CyberShop POS"
#define AppVersion    "1.0.0.3"
#define AppPublisher  "CyberShop"
#define AppExeName    "CyberShopOffline.exe"

[Setup]
AppId={{F2A3B4C5-D6E7-4F89-A0B1-C2D3E4F5A6B7}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
OutputDir=Output
OutputBaseFilename=CyberShopSetup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
DisableProgramGroupPage=yes
DisableDirPage=no
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#AppExeName}
SetupIconFile=assets\cybershop.ico

[Languages]
Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"

[Tasks]
Name: "desktopicon"; Description: "Crear acceso directo en el escritorio"; \
      GroupDescription: "Accesos:"

[Files]
; Output completo de PyInstaller (build_exe.bat genera dist\CyberShopOffline\)
Source: "dist\CyberShopOffline\*"; DestDir: "{app}"; \
        Flags: recursesubdirs createallsubdirs ignoreversion
Source: "assets\cybershop.ico"; DestDir: "{app}\assets"; Flags: ignoreversion
Source: "assets\cybershop.png"; DestDir: "{app}\assets"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}";          Filename: "{app}\{#AppExeName}"; IconFilename: "{app}\assets\cybershop.ico"
Name: "{group}\Desinstalar {#AppName}"; Filename: "{uninstallexe}"
Name: "{commondesktop}\{#AppName}";  Filename: "{app}\{#AppExeName}"; IconFilename: "{app}\assets\cybershop.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Iniciar {#AppName}"; \
          Flags: nowait postinstall skipifsilent

[Code]
var
  ServerPage: TInputQueryWizardPage;
  PgPage:     TInputQueryWizardPage;

(* ────────────────────────────────────────────────────────────
  Helpers para parsear bootstrap.json (parser muy simple, no
  requiere unidades externas). Asume el formato exacto que
  produce installer_packager.py:
    server_url, api_key, tenant_slug — strings JSON simples.
  ──────────────────────────────────────────────────────────── *)

function ExtractJsonString(const Json: AnsiString; const Key: String): String;
var
  Pat: AnsiString;
  P, EndQuote: Integer;
begin
  Result := '';
  Pat := AnsiString('"' + Key + '"');
  P := Pos(Pat, Json);
  if P = 0 then Exit;
  P := P + Length(Pat);
  while (P <= Length(Json)) and (Json[P] <> '"') do Inc(P);
  if P > Length(Json) then Exit;
  Inc(P);  (* saltar la comilla de apertura del valor *)
  EndQuote := P;
  while (EndQuote <= Length(Json)) and (Json[EndQuote] <> '"') do Inc(EndQuote);
  if EndQuote > Length(Json) then Exit;
  Result := String(Copy(Json, P, EndQuote - P));
end;

procedure PreloadFromBootstrap();
var
  BootstrapPath: String;
  Json: AnsiString;
  ServerUrl, ApiKey, TenantSlug: String;
begin
  BootstrapPath := ExpandConstant('{src}\bootstrap.json');
  if not FileExists(BootstrapPath) then Exit;
  if not LoadStringFromFile(BootstrapPath, Json) then Exit;

  ServerUrl  := ExtractJsonString(Json, 'server_url');
  ApiKey     := ExtractJsonString(Json, 'api_key');
  TenantSlug := ExtractJsonString(Json, 'tenant_slug');

  if ServerUrl  <> '' then ServerPage.Values[0] := ServerUrl;
  if ApiKey     <> '' then ServerPage.Values[1] := ApiKey;
  if TenantSlug <> '' then ServerPage.Values[2] := TenantSlug;
end;

procedure InitializeWizard();
begin
  { Página 1: configuración del servidor (obligatoria) }
  ServerPage := CreateInputQueryPage(wpSelectDir,
    'Configuración del servidor',
    'Conexión con CyberShop Cloud',
    'Ingresa los datos del servidor proporcionados por tu proveedor. ' +
    'Si descargaste este instalador desde el portal, los campos se ' +
    'completan automáticamente.');
  ServerPage.Add('URL del servidor:', False);
  ServerPage.Add('API key (X-Sync-Key):', True);
  ServerPage.Add('Slug del tenant:', False);

  { Página 2: Postgres del cliente (opcional, modo avanzado) }
  PgPage := CreateInputQueryPage(ServerPage.ID,
    'Conexión a Postgres (avanzado)',
    'Solo si tu negocio usa una base Postgres propia',
    'Si no aplica, deja todos los campos en blanco. La app sincronizará ' +
    'únicamente vía la API REST del servidor (modo recomendado).');
  PgPage.Add('Host:',         False);
  PgPage.Add('Puerto:',       False);
  PgPage.Add('Base de datos:', False);
  PgPage.Add('Usuario:',      False);
  PgPage.Add('Contraseña:',   True);

  { Pre-llenar Postgres por defecto (puerto 5432) }
  PgPage.Values[1] := '5432';

  PreloadFromBootstrap();
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID = ServerPage.ID then begin
    if Trim(ServerPage.Values[0]) = '' then begin
      MsgBox('La URL del servidor es obligatoria.', mbError, MB_OK);
      Result := False;
    end else if Trim(ServerPage.Values[1]) = '' then begin
      MsgBox('La API key es obligatoria.', mbError, MB_OK);
      Result := False;
    end;
  end;
end;

function NormalizeBool(const V: String): String;
begin
  if (V = '') then Result := 'true' else Result := V;
end;

procedure WriteCybershopConf();
var
  ConfDir, ConfPath: String;
  Lines: TStringList;
begin
  ConfDir  := ExpandConstant('{userappdata}\CyberShopNative');
  ConfPath := ConfDir + '\.cybershop.conf';
  if not DirExists(ConfDir) then ForceDirectories(ConfDir);

  Lines := TStringList.Create;
  try
    Lines.Add('# Configuración generada por el asistente del instalador');
    Lines.Add('# No editar a mano salvo que sepas lo que haces.');
    Lines.Add('SERVER_URL='        + Trim(ServerPage.Values[0]));
    Lines.Add('SYNC_API_KEY='      + Trim(ServerPage.Values[1]));
    Lines.Add('TENANT_SLUG='       + Trim(ServerPage.Values[2]));
    Lines.Add('TENANT_NOMBRE=');
    Lines.Add('PG_HOST='           + Trim(PgPage.Values[0]));
    Lines.Add('PG_PORT='           + Trim(PgPage.Values[1]));
    Lines.Add('PG_DBNAME='         + Trim(PgPage.Values[2]));
    Lines.Add('PG_USER='           + Trim(PgPage.Values[3]));
    Lines.Add('PG_PASSWORD='       + Trim(PgPage.Values[4]));
    Lines.Add('LOCAL_DB_PATH='     + ConfDir + '\cybershop_offline.db');
    Lines.Add('SYNC_INTERVAL_SEC=30');
    Lines.Add('AUTO_UPDATE_CHECK=true');
    Lines.SaveToFile(ConfPath);
  finally
    Lines.Free;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    WriteCybershopConf();
end;

[UninstallDelete]
; No borramos %APPDATA%\CyberShopNative\ por defecto: contiene la BD del
; cliente. Si el usuario quiere limpiar, lo hace a mano.
