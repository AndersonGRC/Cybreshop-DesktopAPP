"""Motor de nómina colombiano para el escritorio (offline).

PORTADO VERBATIM desde la web (app/nomina_engine.py + app/nomina_inteligente.py)
para garantizar que la liquidación offline produzca EXACTAMENTE los mismos
valores que producción. No reinterpretar la matemática: si cambia la web, debe
sincronizarse este archivo.

Normativa: CST, Ley 100/1993, Ley 1607/2012 (exoneración), Ley 50/1990,
Ley 2101/2021 (jornada), Estatuto Tributario Art. 383/388, Ley 2466/2025
(recargo dominical/festivo gradual).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any


# =============================================================================
# Parámetros oficiales (espejo de nomina_inteligente.PARAMETROS_OFICIALES_NOMINA)
# =============================================================================
PARAMETROS_OFICIALES_NOMINA = {
    2025: {"salario_minimo": 1423500.0, "auxilio_transporte": 200000.0, "uvt": 49799.0,
           "estado": "oficial",
           "fuente": "Decreto 1572 de 2024, Decreto 1573 de 2024 y Resolución DIAN 000193 de 2024"},
    2026: {"salario_minimo": 1750905.0, "auxilio_transporte": 249095.0, "uvt": 52374.0,
           "estado": "oficial",
           "fuente": "Decreto 159 de 2026, Decreto 1470 de 2025 y Resolución DIAN 000238 de 2025"},
    2027: {"salario_minimo": 1847205.0, "auxilio_transporte": 262795.0, "uvt": 55256.0,
           "estado": "proyectado",
           "fuente": "Proyección IPC 2026 (~5,5%). Pendiente decreto SMMLV 2027."},
}

JORNADA_LEY_2101 = {2023: 47, 2024: 46, 2025: 44, 2026: 42, 2027: 42}

TABLA_RETENCION_ART_383 = [
    {"rango_desde": 0, "rango_hasta": 95, "tarifa_marginal": 0.0, "uvt_mas": 0, "uvt_base": 0},
    {"rango_desde": 95, "rango_hasta": 150, "tarifa_marginal": 19.0, "uvt_mas": 0, "uvt_base": 95},
    {"rango_desde": 150, "rango_hasta": 360, "tarifa_marginal": 28.0, "uvt_mas": 10, "uvt_base": 150},
    {"rango_desde": 360, "rango_hasta": 640, "tarifa_marginal": 33.0, "uvt_mas": 69, "uvt_base": 360},
    {"rango_desde": 640, "rango_hasta": 945, "tarifa_marginal": 35.0, "uvt_mas": 162, "uvt_base": 640},
    {"rango_desde": 945, "rango_hasta": 2300, "tarifa_marginal": 37.0, "uvt_mas": 268, "uvt_base": 945},
    {"rango_desde": 2300, "rango_hasta": float("inf"), "tarifa_marginal": 39.0, "uvt_mas": 770, "uvt_base": 2300},
]

ARL_NIVELES = {"I": 0.522, "II": 1.044, "III": 2.436, "IV": 4.350, "V": 6.960}

BASE_HORAS_MENSUAL = 240

TIPOS_EXTRAS = {"HED", "HEN", "HEDF", "HENF", "RN", "RD"}
TIPOS_LICENCIAS_REMUNERADAS = {"INCAPACIDAD_GEN", "INCAPACIDAD_LAB", "LICENCIA_MAT", "LICENCIA_PAT", "LICENCIA_LUTO"}
TIPOS_LICENCIAS_NO_REMUNERADAS = {"LICENCIA_NR"}
TIPOS_SOPORTADOS = TIPOS_EXTRAS | TIPOS_LICENCIAS_REMUNERADAS | TIPOS_LICENCIAS_NO_REMUNERADAS


# =============================================================================
# Recargos y horas extras (Art. 168-179 CST) con dominical/festivo Ley 2466/2025
# =============================================================================
_FACTORES_FIJOS = {"HED": 1.25, "HEN": 1.75, "HEDF": 2.00, "HENF": 2.50, "RN": 0.35}


def factor_recargo_dominical(fecha: date | None) -> float:
    """Recargo dominical/festivo (RD) según Ley 2466 de 2025 (gradual):
       75% hasta jun-2025 · 80% desde jul-1-2025 · 90% desde jul-1-2026 · 100% desde jul-1-2027."""
    if not fecha:
        fecha = date.today()
    if fecha >= date(2027, 7, 1):
        return 1.00
    if fecha >= date(2026, 7, 1):
        return 0.90
    if fecha >= date(2025, 7, 1):
        return 0.80
    return 0.75


def factores_horas_extras(fecha: date | None = None) -> dict:
    f = dict(_FACTORES_FIJOS)
    f["RD"] = factor_recargo_dominical(fecha)
    return f


def calcular_valor_hora(salario_base):
    return salario_base / BASE_HORAS_MENSUAL


def calcular_horas_extras(valor_hora, tipo, cantidad, fecha: date | None = None):
    factor = factores_horas_extras(fecha).get(tipo, 1.0)
    return valor_hora * factor * cantidad


def calcular_auxilio_transporte(salario_base, smmlv, valor_auxilio):
    if salario_base <= (2 * smmlv):
        return valor_auxilio
    return 0


def calcular_salud_pension(base_cotizacion, porcentaje_salud, porcentaje_pension):
    salud = base_cotizacion * (porcentaje_salud / 100)
    pension = base_cotizacion * (porcentaje_pension / 100)
    return salud, pension


def calcular_fondo_solidaridad(base_cotizacion, smmlv):
    if smmlv == 0:
        return 0
    veces_smmlv = base_cotizacion / smmlv
    porcentaje = 0
    if 4 <= veces_smmlv < 16:
        porcentaje = 1.0
    elif 16 <= veces_smmlv < 17:
        porcentaje = 1.2
    elif 17 <= veces_smmlv < 18:
        porcentaje = 1.4
    elif 18 <= veces_smmlv < 19:
        porcentaje = 1.6
    elif 19 <= veces_smmlv < 20:
        porcentaje = 1.8
    elif veces_smmlv >= 20:
        porcentaje = 2.0
    return base_cotizacion * (porcentaje / 100)


def calcular_retencion_fuente(ingreso_laboral, salud_pension_fsp, uvt_valor, tabla_retencion):
    base_depurada = ingreso_laboral - salud_pension_fsp
    if base_depurada < 0:
        base_depurada = 0
    renta_exenta = base_depurada * 0.25
    tope_renta_exenta = (790 / 12) * uvt_valor
    if renta_exenta > tope_renta_exenta:
        renta_exenta = tope_renta_exenta
    base_gravable_pesos = base_depurada - renta_exenta
    base_gravable_uvt = base_gravable_pesos / uvt_valor
    retencion_uvt = 0
    for rango in tabla_retencion:
        desde = rango["rango_desde"]
        hasta = rango["rango_hasta"]
        if desde <= base_gravable_uvt < hasta:
            tarifa = rango["tarifa_marginal"]
            uvt_mas = rango["uvt_mas"]
            uvt_base = rango["uvt_base"]
            retencion_uvt = ((base_gravable_uvt - uvt_base) * (tarifa / 100)) + uvt_mas
            break
    return retencion_uvt * uvt_valor


def dias_360(fecha_inicio, fecha_fin, inclusivo=True):
    if not fecha_inicio or not fecha_fin:
        return 0
    dias = (fecha_fin.year - fecha_inicio.year) * 360 + \
           (fecha_fin.month - fecha_inicio.month) * 30 + \
           (min(fecha_fin.day, 30) - min(fecha_inicio.day, 30))
    if inclusivo:
        dias += 1
    return max(0, dias)


# =============================================================================
# Helpers de liquidación (espejo de nomina_inteligente, sin pandas)
# =============================================================================
def _to_float(value, default=0.0):
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_date(value):
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value)[:19]).date()
    except ValueError:
        try:
            return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
        except ValueError:
            return None


def _money(value):
    return round(_to_float(value), 2)


def _alert(level, message, empleado_id=None):
    payload = {"nivel": level, "mensaje": message}
    if empleado_id is not None:
        payload["empleado_id"] = empleado_id
    return payload


def _dias_periodo(periodo):
    fi = _to_date(periodo.get("fecha_inicio"))
    ff = _to_date(periodo.get("fecha_fin"))
    if fi and ff:
        return dias_360(fi, ff)
    return 15 if periodo.get("numero_periodo") in (1, 2) else 30


def _dias_trabajados_en_periodo(empleado, fecha_inicio, fecha_fin, dias_periodo):
    fecha_ingreso = _to_date(empleado.get("fecha_ingreso"))
    if not fecha_inicio or not fecha_fin or not fecha_ingreso:
        return dias_periodo
    if fecha_ingreso > fecha_fin:
        return 0
    if fecha_ingreso > fecha_inicio:
        return dias_360(fecha_ingreso, fecha_fin)
    return dias_periodo


def _agrupar_novedades(novedades):
    agregadas = {}
    tipos = set()
    for nov in novedades:
        eid = int(_to_float(nov.get("empleado_id"), 0))
        if eid <= 0:
            continue
        tipo = str(nov.get("tipo_novedad") or "").strip().upper()
        if not tipo:
            continue
        bucket = agregadas.setdefault(eid, {})
        bucket[f"cantidad_{tipo}"] = bucket.get(f"cantidad_{tipo}", 0.0) + _to_float(nov.get("cantidad"))
        bucket[f"valor_total_{tipo}"] = bucket.get(f"valor_total_{tipo}", 0.0) + _to_float(nov.get("valor_total"))
        tipos.add(tipo)
    return agregadas, tipos


def _parametros_normativos(periodo, params):
    anio = int(_to_float(periodo.get("anio"), 0))
    oficiales = PARAMETROS_OFICIALES_NOMINA.get(anio)
    if not oficiales:
        return []
    alertas = []
    for clave, etiqueta in (("salario_minimo", "salario mínimo"),
                            ("auxilio_transporte", "auxilio de transporte"), ("uvt", "UVT")):
        actual = _to_float(params.get(clave))
        oficial = _to_float(oficiales.get(clave))
        if abs(actual - oficial) > 1:
            alertas.append(_alert("warning",
                f"El parámetro {etiqueta} {anio} no coincide con el oficial. "
                f"Configurado: ${actual:,.0f}. Oficial: ${oficial:,.0f}. Fuente: {oficiales['fuente']}."))
    return alertas


def _retencion_periodo(ingreso_gravable, deducciones, uvt, dias_periodo):
    if ingreso_gravable <= 0 or uvt <= 0 or dias_periodo <= 0:
        return 0.0
    factor = 30 / dias_periodo
    ret_mensual = calcular_retencion_fuente(ingreso_gravable * factor, deducciones * factor, uvt, TABLA_RETENCION_ART_383)
    return _money(ret_mensual / factor)


def _seguridad_social_contratista(honorarios_periodo, smmlv, nivel_arl):
    alertas = []
    if honorarios_periodo <= 0:
        return {"base": 0.0, "salud": 0.0, "pension": 0.0, "arl": 0.0, "arl_empresa": 0.0, "total": 0.0}, alertas
    base = honorarios_periodo * 0.40
    if base < smmlv:
        base = smmlv
    base = min(base, smmlv * 25) if smmlv > 0 else base
    salud = base * 0.125
    pension = base * 0.16
    arl_pct = ARL_NIVELES.get(nivel_arl, ARL_NIVELES["I"])
    arl = base * (arl_pct / 100)
    arl_empresa = 0.0
    if nivel_arl in {"IV", "V"}:
        arl_empresa = arl
        arl = 0.0
        alertas.append(_alert("warning", "Contratista ARL IV/V: la ARL no se descontó (corresponde al contratante)."))
    return {"base": _money(base), "salud": _money(salud), "pension": _money(pension),
            "arl": _money(arl), "arl_empresa": _money(arl_empresa),
            "total": _money(salud + pension + arl)}, alertas


def liquidar_periodo(periodo: dict, params: dict, empleados: list, novedades: list) -> dict:
    """Liquidación del período (Python puro). ESPEJO EXACTO de
    nomina_inteligente._calcular_nomina_periodo_fallback. Devuelve
    {detalles, alertas, resumen}."""
    smmlv = _to_float(params.get("salario_minimo"))
    aux_transporte = _to_float(params.get("auxilio_transporte"))
    uvt = _to_float(params.get("uvt"))
    fecha_inicio = _to_date(periodo.get("fecha_inicio"))
    fecha_fin = _to_date(periodo.get("fecha_fin"))
    dias_periodo = _dias_periodo(periodo)

    alertas = _parametros_normativos(periodo, params)
    novedades_por_empleado, tipos_detectados = _agrupar_novedades(novedades)
    for tipo in sorted(tipos_detectados - TIPOS_SOPORTADOS):
        alertas.append(_alert("warning",
            f"La novedad {tipo} no tiene regla automática; se trató como devengado salarial manual."))

    detalles = []
    total_empleados = 0
    total_contratistas = 0

    for empleado in empleados:
        empleado_id = int(_to_float(empleado.get("id"), 0))
        if empleado_id <= 0:
            continue
        tipo_vinculacion = str(empleado.get("tipo_vinculacion") or "").upper()
        salario_base = _to_float(empleado.get("salario_base"))
        dias_trabajados = _dias_trabajados_en_periodo(empleado, fecha_inicio, fecha_fin, dias_periodo)
        novedades_emp = novedades_por_empleado.get(empleado_id, {})

        if tipo_vinculacion == "CONTRATISTA":
            total_contratistas += 1
            nivel_arl = str(empleado.get("nivel_arl") or "I").strip().upper()
            honorarios_periodo = salario_base * (dias_trabajados / 30) if dias_trabajados else 0.0
            ss, alertas_c = _seguridad_social_contratista(honorarios_periodo, smmlv, nivel_arl)
            nombre = f"{empleado.get('nombres', '')} {empleado.get('apellidos', '')}".strip()
            for a in alertas_c:
                a["empleado_id"] = empleado_id
                a["mensaje"] = f"{nombre}: {a['mensaje']}"
                alertas.append(a)
            detalles.append({
                "empleado_id": empleado_id, "dias_trabajados": dias_trabajados,
                "sueldo_basico": _money(honorarios_periodo), "auxilio_transporte": 0.0,
                "horas_extras": 0.0, "total_devengado": _money(honorarios_periodo),
                "salud_empleado": ss["salud"], "pension_empleado": ss["pension"],
                "fondo_solidaridad": ss["arl"], "retencion_fuente": 0.0,
                "total_deducido": ss["total"], "neto_pagar": _money(honorarios_periodo),
            })
            continue

        total_empleados += 1
        extras = sum(_to_float(novedades_emp.get(f"valor_total_{t}")) for t in TIPOS_EXTRAS)
        pagos_licencias = sum(_to_float(novedades_emp.get(f"valor_total_{t}")) for t in TIPOS_LICENCIAS_REMUNERADAS)
        dias_lic_rem = sum(_to_float(novedades_emp.get(f"cantidad_{t}")) for t in TIPOS_LICENCIAS_REMUNERADAS)
        dias_lic_nr = sum(_to_float(novedades_emp.get(f"cantidad_{t}")) for t in TIPOS_LICENCIAS_NO_REMUNERADAS)
        otros_devengados = sum(_to_float(v) for k, v in novedades_emp.items()
                               if k.startswith("valor_total_") and k.replace("valor_total_", "") not in TIPOS_SOPORTADOS)

        total_dias_novedad = dias_lic_rem + dias_lic_nr
        if total_dias_novedad > dias_trabajados:
            nombre = f"{empleado.get('nombres', '')} {empleado.get('apellidos', '')}".strip()
            alertas.append(_alert("warning",
                f"{nombre}: las novedades suman {total_dias_novedad:.1f} días y exceden los {dias_trabajados} del periodo.",
                empleado_id=empleado_id))
        dias_lic_rem = min(dias_lic_rem, dias_trabajados)
        dias_lic_nr = min(dias_lic_nr, max(dias_trabajados - dias_lic_rem, 0))
        dias_basico = max(dias_trabajados - dias_lic_rem - dias_lic_nr, 0)

        basico = salario_base * (dias_basico / 30) if dias_basico else 0.0
        aux_base = calcular_auxilio_transporte(salario_base, smmlv, aux_transporte)
        aux_trans = aux_base * (dias_basico / 30) if aux_base and dias_basico else 0.0

        if tipo_vinculacion == "APRENDIZ_SENA":
            alertas.append(_alert("warning",
                f"Empleado {empleado_id}: cálculo de aprendiz SENA es referencial; revise apoyo de sostenimiento.",
                empleado_id=empleado_id))
            aux_trans = 0.0
            salud_emp = pension_emp = fsp = retencion = 0.0
        else:
            if salario_base < smmlv:
                alertas.append(_alert("warning",
                    f"Empleado {empleado_id}: salario base (${salario_base:,.0f}) inferior al SMMLV (${smmlv:,.0f}).",
                    empleado_id=empleado_id))
            base_ss = basico + extras + pagos_licencias + otros_devengados
            salud_emp, pension_emp = calcular_salud_pension(base_ss, 4, 4)
            fsp = calcular_fondo_solidaridad(base_ss, smmlv)
            retencion = _retencion_periodo(base_ss, salud_emp + pension_emp + fsp, uvt, dias_periodo)

        total_devengado = basico + aux_trans + extras + pagos_licencias + otros_devengados
        total_deducido = salud_emp + pension_emp + fsp + retencion
        neto = total_devengado - total_deducido
        detalles.append({
            "empleado_id": empleado_id, "dias_trabajados": dias_trabajados,
            "sueldo_basico": _money(basico), "auxilio_transporte": _money(aux_trans),
            "horas_extras": _money(extras), "total_devengado": _money(total_devengado),
            "salud_empleado": _money(salud_emp), "pension_empleado": _money(pension_emp),
            "fondo_solidaridad": _money(fsp), "retencion_fuente": _money(retencion),
            "total_deducido": _money(total_deducido), "neto_pagar": _money(neto),
        })

    resumen = {
        "empleados": total_empleados, "contratistas": total_contratistas,
        "total_devengado": _money(sum(d["total_devengado"] for d in detalles)),
        "total_neto": _money(sum(d["neto_pagar"] for d in detalles)),
    }
    return {"detalles": detalles, "alertas": alertas, "resumen": resumen}
