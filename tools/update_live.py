#!/usr/bin/env python3
"""Actualiza data/live.json con series de FUENTES OFICIALES GRATUITAS (solo biblioteca estandar de Python).

Uso:   python tools/update_live.py [salida.json] [--only id1,id2] [--no-log]
Claves opcionales (variables de entorno, NUNCA dentro del APK): FMP_API_KEY (Brent 1er mes y USD/INR de la edicion),  EIA_API_KEY (si falta se usa el archivo masivo oficial PET.zip, sin clave; la DEMO_KEY publica quedo descartada: devuelve HTTP 429), FRED_API_KEY.

Reglas de diseno
  - Cada serie conserva su propia definicion y fuente; jamas se sustituye una serie por otra (p. ej. Brent EIA = spot FOB Europa, NO el ICE 1er mes del radar.json).
  - Todo es CIERRE/FIJACION diaria, semanal o mensual: ninguna fuente gratuita verificada entrega tiempo real ni diferido de bolsa.
  - No hay scraping de sitios que lo prohiban; solo APIs/archivos de datos abiertos documentados.
  - Validaciones: fechas, unidades/definicion, rangos plausibles, orden, fechas futuras, datos congelados, frescura segun calendario.
  - Se conserva el historial previo (merge por fecha) y se registra fecha anterior vs nueva por indicador.
"""
import csv, io, json, os, re, sys, time, zipfile, datetime as dt
import urllib.request, urllib.parse, urllib.error
import xml.etree.ElementTree as ET

UA = {"User-Agent": "magdalena-finanzas-live/2.0 (open-data client)"}
GT = dt.timezone(dt.timedelta(hours=-6))                       # Guatemala: UTC-6 todo el ano
NOW = dt.datetime.now(dt.timezone.utc)
TODAY_GT = NOW.astimezone(GT).date()
LOG = []

# ---------- calendarios de referencia (feriados principales; no exhaustivos) ----------
HOL = {
 "US": "2026-01-01 2026-01-19 2026-02-16 2026-04-03 2026-05-25 2026-06-19 2026-07-03 2026-09-07 2026-10-12 2026-11-11 2026-11-26 2026-12-25 2027-01-01 2027-01-18 2027-02-15 2027-03-26 2027-05-31 2027-06-18 2027-07-05 2027-09-06 2027-10-11 2027-11-11 2027-11-25 2027-12-24",
 "BR": "2026-01-01 2026-02-16 2026-02-17 2026-04-03 2026-04-21 2026-05-01 2026-06-04 2026-09-07 2026-10-12 2026-11-02 2026-11-20 2026-12-25 2027-01-01 2027-02-08 2027-02-09 2027-03-26 2027-04-21 2027-05-27 2027-09-07 2027-10-12 2027-11-02 2027-11-15 2027-12-25",
 "GT": "2026-01-01 2026-04-02 2026-04-03 2026-05-01 2026-06-30 2026-09-15 2026-10-20 2026-11-01 2026-12-25 2027-01-01 2027-03-25 2027-03-26 2027-05-01 2027-06-30 2027-09-15 2027-10-20 2027-11-01 2027-12-25",
 "EU": "2026-01-01 2026-04-03 2026-04-06 2026-05-01 2026-12-25 2026-12-26 2027-01-01 2027-03-26 2027-03-29 2027-12-27",
}
HOL = {k: {dt.date.fromisoformat(x) for x in v.split()} for k, v in HOL.items()}

def is_bd(d, cal): return d.weekday() < 5 and d not in HOL.get(cal, set())
def prev_bd(d, cal, n=1):
    while n > 0:
        d -= dt.timedelta(days=1)
        if is_bd(d, cal): n -= 1
    return d
def last_bd_on_or_before(d, cal):
    while not is_bd(d, cal): d -= dt.timedelta(days=1)
    return d
def expected_last(today, cal, lag_bd):
    """Ultima fecha de dato que ya deberia estar publicada hoy (calendario habil + rezago de publicacion)."""
    return prev_bd(last_bd_on_or_before(today, cal), cal, lag_bd) if lag_bd else last_bd_on_or_before(today, cal)
def bd_between(a, b, cal):
    n, d = 0, a
    while d < b:
        d += dt.timedelta(days=1)
        if is_bd(d, cal): n += 1
    return n

# ---------- HTTP ----------
def http(url, data=None, headers=None, timeout=60, retries=2, sid=""):
    h = dict(UA); h.update(headers or {})
    last = None
    for i in range(retries + 1):
        t0 = time.time()
        try:
            req = urllib.request.Request(url, data=data, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
            LOG.append({"id": sid, "url": url.split("?")[0], "http": 200, "bytes": len(body), "ms": int((time.time() - t0) * 1000), "ok": True})
            return body
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            LOG.append({"id": sid, "url": url.split("?")[0], "http": e.code, "ms": int((time.time() - t0) * 1000), "ok": False})
            if e.code in (400, 401, 403, 404): break
        except Exception as e:
            last = str(e)[:120]
            LOG.append({"id": sid, "url": url.split("?")[0], "http": None, "ms": int((time.time() - t0) * 1000), "ok": False, "err": last})
        time.sleep(1.5 * (i + 1))
    raise RuntimeError(last or "error de red")

def jget(url, **k): return json.loads(http(url, **k).decode("utf-8"))

# ---------- conectores ----------
def c_nyfed(path):
    def run():
        start = (TODAY_GT - dt.timedelta(days=730)).isoformat()
        j = jget(f"https://markets.newyorkfed.org/api/rates/{path}/search.json?startDate={start}&endDate={TODAY_GT.isoformat()}", sid=path)
        rows = j["refRates"]
        want = "SOFR" if "sofr" in path else "EFFR"
        assert all(r["type"] == want for r in rows), "tipo de tasa inesperado"
        return {"pts": [(r["effectiveDate"], float(r["percentRate"])) for r in rows if r.get("percentRate") is not None]}
    return run

def c_bcb_ptax():
    d0 = (TODAY_GT - dt.timedelta(days=730)).strftime("%m-%d-%Y"); d1 = TODAY_GT.strftime("%m-%d-%Y")
    u = ("https://olinda.bcb.gov.br/olinda/servico/PTAX/versao/v1/odata/CotacaoDolarPeriodo(dataInicial=@dataInicial,dataFinalCotacao=@dataFinalCotacao)"
         f"?@dataInicial='{d0}'&@dataFinalCotacao='{d1}'&$format=json&$select=cotacaoCompra,cotacaoVenda,dataHoraCotacao&$orderby=dataHoraCotacao asc")
    rows = jget(u.replace(" ", "%20"), sid="bcb_ptax")["value"]
    pts, last_time = [], None
    for r in rows:
        assert r["cotacaoVenda"] >= r["cotacaoCompra"] > 0, "PTAX venta < compra"
        d = r["dataHoraCotacao"][:10]; pts.append((d, float(r["cotacaoVenda"])))
        last_time = r["dataHoraCotacao"][:19]
    return {"pts": pts, "asof_time": last_time.replace(" ", "T") + "-03:00" if last_time else None}

def c_banguat():
    soap = ('<?xml version="1.0" encoding="utf-8"?><soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body>'
            '<TipoCambioFechaInicial xmlns="http://www.banguat.gob.gt/variables/ws/"><fechainit>%s</fechainit></TipoCambioFechaInicial></soap:Body></soap:Envelope>'
            % (TODAY_GT - dt.timedelta(days=730)).strftime("%d/%m/%Y")).encode()
    x = http("https://www.banguat.gob.gt/variables/ws/TipoCambio.asmx", data=soap, sid="banguat",
             headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": '"http://www.banguat.gob.gt/variables/ws/TipoCambioFechaInicial"'})
    root = ET.fromstring(x); ns = {"w": "http://www.banguat.gob.gt/variables/ws/"}
    pts, ahead = [], []
    for v in root.iter("{http://www.banguat.gob.gt/variables/ws/}Var"):
        assert v.find("w:moneda", ns).text == "2", "moneda != USD"
        d, m, y = v.find("w:fecha", ns).text.split("/"); iso = f"{y}-{m}-{d}"; val = float(v.find("w:venta", ns).text)
        (ahead if dt.date.fromisoformat(iso) > TODAY_GT else pts).append((iso, val))
    return {"pts": pts, "extra": {"publicado_por_adelantado": [list(a) for a in ahead]}}

_EIA_BULK = {}
def _eia_bulk(series_id):
    """Archivo masivo oficial de EIA (PET.zip, ~55 MB, SIN clave, JSON por linea). Se descarga una sola vez por corrida."""
    if "zip" not in _EIA_BULK:
        _EIA_BULK["zip"] = http("https://www.eia.gov/opendata/bulk/PET.zip", sid="eia_bulk_PET", timeout=300, retries=1)
    z = zipfile.ZipFile(io.BytesIO(_EIA_BULK["zip"]))
    want = f'"series_id":"PET.{series_id}.D"'.encode()
    with z.open(z.namelist()[0]) as f:
        for line in f:
            if want in line.replace(b'": "', b'":"'):
                j = json.loads(line)
                assert j["units"] == "Dollars per Barrel" and j["f"] == "D", "serie/unidad EIA inesperada"
                since = (TODAY_GT - dt.timedelta(days=730)).strftime("%Y%m%d")        # misma ventana de 2 anos que las demas fuentes (el archivo trae desde 1986)
                pts = [(f"{d[:4]}-{d[4:6]}-{d[6:]}", float(v)) for d, v in j["data"] if v not in (None, "") and d >= since]
                return {"pts": pts, "extra": {"clave": "sin clave (archivo masivo oficial EIA PET.zip)", "descripcion": j["name"], "actualizado_por_eia": j.get("last_updated")}}
    raise RuntimeError("serie no encontrada en PET.zip")

def c_eia(series_id):
    def run():
        key = os.environ.get("EIA_API_KEY")
        if key:
            start = (TODAY_GT - dt.timedelta(days=730)).isoformat()
            q = urllib.parse.urlencode({"api_key": key, "frequency": "daily", "data[0]": "value", "facets[series][]": series_id,
                                        "sort[0][column]": "period", "sort[0][direction]": "asc", "start": start, "length": 5000})
            try:
                rows = jget("https://api.eia.gov/v2/petroleum/pri/spt/data/?" + q, sid="eia_" + series_id)["response"]["data"]
                assert rows and all(r["series"] == series_id and r["units"] == "$/BBL" for r in rows), "serie/unidad EIA inesperada"
                return {"pts": [(r["period"], float(r["value"])) for r in rows if r.get("value") not in (None, "")],
                        "extra": {"clave": "EIA_API_KEY propia", "descripcion": rows[0].get("series-description")}}
            except Exception as e:
                LOG.append({"id": "eia_" + series_id, "ok": False, "err": "API con clave fallo, se usa el archivo masivo: " + str(e)[:80]})
        return _eia_bulk(series_id)                              # sin clave (o API caida/limitada): archivo masivo oficial
    return run

def c_ecb_inr():
    def one(cur):
        j = jget(f"https://data-api.ecb.europa.eu/service/data/EXR/D.{cur}.EUR.SP00.A?startPeriod={(TODAY_GT - dt.timedelta(days=730)).isoformat()}&format=jsondata", sid="ecb_" + cur)
        times = [v["id"] for v in j["structure"]["dimensions"]["observation"][0]["values"]]
        ser = next(iter(j["dataSets"][0]["series"].values()))["observations"]
        return {times[int(k)]: v[0] for k, v in ser.items()}
    usd, inr = one("USD"), one("INR")
    return {"pts": [(d, round(inr[d] / usd[d], 4)) for d in sorted(inr) if d in usd],
            "extra": {"derivado": "EUR/INR dividido entre EUR/USD (referencia BCE ~14:15 CET); no es un cierre de mercado"}}

BROWSER_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
              "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,text/csv,*/*;q=0.8", "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.7"}
ANP_BACKOFF = (5, 15, 40)                                      # segundos entre reintentos (gov.br resetea conexiones de IP de nube de forma intermitente)

def _anp_stream(url, acc):
    """Descarga un CSV de ANP con reintentos y espera creciente; headers de navegador. Las filas se indexan por (CNPJ, fecha), asi que reintentar es idempotente."""
    last = None
    for i in range(len(ANP_BACKOFF) + 1):
        try: return _anp_stream_once(url, acc)
        except Exception as e:
            last = e
            LOG.append({"id": "anp", "url": url.split("?")[0].split("/")[-1], "ok": False, "intento": i + 1, "err": str(e)[:100]})
            if i < len(ANP_BACKOFF): time.sleep(ANP_BACKOFF[i])
    raise last

def _anp_stream_once(url, acc):
    """Lee un CSV de ANP en streaming (los archivos mensuales pesan decenas de MB) y acumula precio de ETANOL por semana."""
    t0 = time.time()
    req = urllib.request.Request(url, headers=BROWSER_UA)
    n = 0
    with urllib.request.urlopen(req, timeout=180) as r:
        rd = csv.reader(io.TextIOWrapper(r, encoding="utf-8-sig", errors="replace", newline=""), delimiter=";")
        head = [h.strip() for h in next(rd)]
        iP, iD, iV, iU = head.index("Produto"), head.index("Data da Coleta"), head.index("Valor de Venda"), head.index("Unidade de Medida")
        iC = head.index("CNPJ da Revenda")
        for row in rd:
            if len(row) <= max(iP, iD, iV, iU, iC) or row[iP].strip().upper() != "ETANOL": continue
            assert row[iU].replace(" ", "").lower().startswith("r$/l"), "unidad ANP inesperada: " + row[iU]
            try:
                d = dt.datetime.strptime(row[iD].strip(), "%d/%m/%Y").date(); v = float(row[iV].replace(",", "."))
            except ValueError: continue
            if not 0.5 < v < 20: continue
            sat = d + dt.timedelta(days=(5 - d.weekday()) % 7)            # sabado de cierre de la semana de recoleccion
            s = acc.setdefault(sat.isoformat(), {})
            s[(row[iC].strip(), row[iD].strip())] = v          # una observacion por bomba y fecha (evita duplicados entre archivos)
            n += 1
    LOG.append({"id": "anp", "url": url.split("?")[0].split("/")[-1], "http": 200, "rows_etanol": n, "ms": int((time.time() - t0) * 1000), "ok": True})

def c_anp():
    """Etanol hidratado al consumidor: promedio simple por semana (domingo-sabado) del Levantamiento de Precios de ANP (CSV abierto).
    Fuentes: archivo 'ultimas 4 semanas' + los archivos mensuales mas recientes listados en la pagina de dados abertos."""
    page = None
    for i in range(len(ANP_BACKOFF) + 1):
        try:
            page = http("https://www.gov.br/anp/pt-br/centrais-de-conteudo/dados-abertos/serie-historica-de-precos-de-combustiveis", sid="anp_page", timeout=90, retries=0, headers=BROWSER_UA).decode("utf-8", "replace"); break
        except Exception:
            if i == len(ANP_BACKOFF): raise
            time.sleep(ANP_BACKOFF[i])
    root = "https://www.gov.br/anp/pt-br/centrais-de-conteudo/dados-abertos/arquivos/shpc/"
    monthly = sorted({m for m in re.findall(r'href="(https://www\.gov\.br/anp/[^"]*/shpc/dsan/%d/[^"]*gasolina-etanol\.csv)"' % TODAY_GT.year, page)},
                     key=lambda u: int(re.search(r"/(\d{2})-", u).group(1)) if re.search(r"/(\d{2})-", u) else 0)
    urls = monthly[-2:] + [root + "qus/ultimas-4-semanas-gasolina-etanol.csv"]
    acc = {}
    for u in urls:
        try: _anp_stream(u, acc)
        except Exception as e: LOG.append({"id": "anp", "url": u.split("/")[-1], "ok": False, "err": str(e)[:100]})
    if not acc: raise RuntimeError("sin datos de etanol en los CSV de ANP")
    pts = []
    for k, rows in sorted(acc.items()):
        vals = list(rows.values())
        if len(vals) >= 500: pts.append((k, round(sum(vals) / len(vals), 4)))
    return {"pts": pts, "extra": {"n_precios_por_semana": {k: len(v) for k, v in sorted(acc.items())}, "archivos": [u.split("/")[-1] for u in urls],
            "definicion": "promedio simple de precios de venta al consumidor (producto ETANOL = hidratado) de las bombas encuestadas; semana domingo-sabado, fecha = sabado"}}

def xlsx_sheet(raw, sheet_name):
    z = zipfile.ZipFile(io.BytesIO(raw))
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main", "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
    ss = [("".join(t.text or "" for t in si.iter("{%s}t" % ns["m"]))) for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", ns)]
    wb = ET.fromstring(z.read("xl/workbook.xml")); rid = None
    for s in wb.find("m:sheets", ns):
        if s.get("name") == sheet_name: rid = s.get("{%s}id" % ns["r"])
    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels")); tgt = next(r.get("Target") for r in rels if r.get("Id") == rid)
    sh = ET.fromstring(z.read("xl/" + tgt.lstrip("/").replace("xl/", "")))
    rows = {}
    for c in sh.iter("{%s}c" % ns["m"]):
        ref = c.get("r"); col = re.match(r"[A-Z]+", ref).group(); rn = int(ref[len(col):]); v = c.find("m:v", ns)
        if v is None: continue
        val = ss[int(v.text)] if c.get("t") == "s" else v.text
        rows.setdefault(rn, {})[col] = val
    return rows

def c_worldbank(names):
    """Banco Mundial, Commodity Price Data (Pink Sheet) mensual. Hoja 'Monthly Prices': columnas = productos (fila 5), unidades (fila 6), filas = meses 'YYYYMmm'."""
    page = http("https://www.worldbank.org/en/research/commodity-markets", sid="wb_page", timeout=60).decode("utf-8", "replace")
    link = re.search(r'href="(https://thedocs\.worldbank\.org/[^"]+CMO-Historical-Data-Monthly\.xlsx)"', page).group(1)
    raw = http(link, sid="wb_xlsx", timeout=120)
    rows = xlsx_sheet(raw, "Monthly Prices")
    hdr_rn = next(rn for rn, r in sorted(rows.items()) if "Crude oil, Brent" in r.values())
    names_row, units_row = rows[hdr_rn], rows.get(hdr_rn + 1, {})
    updated = next((r["A"] for rn, r in sorted(rows.items()) if str(r.get("A", "")).startswith("Updated")), None)
    out = {}
    for want, key in names:
        col = next((c for c, v in names_row.items() if str(v).strip() == want), None)
        if col is None: continue
        pts = []
        for rn, r in rows.items():
            m = re.fullmatch(r"(\d{4})M(\d{2})", str(r.get("A", "")))
            if not m or col not in r: continue
            try: pts.append((f"{m.group(1)}-{m.group(2)}-01", float(r[col])))
            except ValueError: pass
        out[key] = {"pts": sorted(pts), "extra": {"columna": want, "unidad_en_archivo": units_row.get(col), "actualizado_por_el_banco": updated, "archivo": link}}
    return out

def c_fred(series):
    def run():
        key = os.environ.get("FRED_API_KEY")
        if not key: raise RuntimeError("FRED_API_KEY no definida (clave gratuita; conector sin probar)")
        j = jget("https://api.stlouisfed.org/fred/series/observations?" + urllib.parse.urlencode({"series_id": series, "api_key": key, "file_type": "json",
                 "observation_start": (TODAY_GT - dt.timedelta(days=730)).isoformat()}), sid="fred_" + series)
        return {"pts": [(o["date"], float(o["value"])) for o in j["observations"] if o["value"] not in (".", "")]}
    return run

def c_fmp(symbol):
    """FMP (Financial Modeling Prep), API 'stable' documentada: /stable/historical-price-eod/light?symbol=...&from=...&to=...&apikey=...
    Requiere FMP_API_KEY (clave PROPIA del usuario, solo en el entorno/secretos del pipeline, nunca en el APK). Sin clave no consulta nada."""
    def run():
        key = os.environ.get("FMP_API_KEY")
        if not key: raise RuntimeError("FMP_API_KEY no definida: FMP no se consulta (se conserva el ultimo dato valido)")
        q = urllib.parse.urlencode({"symbol": symbol, "from": (TODAY_GT - dt.timedelta(days=370)).isoformat(), "to": TODAY_GT.isoformat(), "apikey": key})
        rows = jget("https://financialmodelingprep.com/stable/historical-price-eod/light?" + q, sid="fmp_" + symbol)
        assert isinstance(rows, list) and rows and all(r.get("symbol") == symbol for r in rows), "respuesta FMP inesperada"
        pts = [(r["date"], float(r["price"])) for r in rows if r.get("price") is not None and dt.date.fromisoformat(r["date"]).weekday() < 5]   # FX trae marcas de apertura dominical: se descartan
        return {"pts": pts, "extra": {"metodo": "API FMP con clave propia (FMP_API_KEY)"}}
    return run

# ---------- CEPEA/ESALQ (datos abiertos con licencia CC BY-NC 4.0 declarada en la propia pagina; robots.txt: User-agent * Allow /) ----------
import re as _re, html as _html, unicodedata as _ud
def _norm(x): return _ud.normalize("NFD", x).encode("ascii", "ignore").decode().upper()
def _cl(x): return _html.unescape(_re.sub(r"\s+", " ", _re.sub(r"<[^>]+>", " ", x))).strip()
CEPEA_KEYS = ["HIDRATADO COMBUSTIVEL", "ANIDRO", "HIDRATADO OUTROS FINS", "CRISTAL BRANCO", "CRISTAL - SANTOS"]
def cepea_tables(raw):
    """-> {clave: filas}. La clave es el encabezado mas cercano ANTES de cada tabla (el ultimo que aparece en el texto previo)."""
    out = {}
    for m in _re.finditer(r"<table.*?</table>", raw, _re.S | _re.I):
        before = _norm(_cl(raw[max(0, m.start() - 1500):m.start()]))
        pos = {k: before.rfind(k) for k in CEPEA_KEYS}; k = max(pos, key=pos.get)
        if pos[k] < 0: continue
        rows = [[_cl(c) for c in _re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, _re.S | _re.I)] for r in _re.findall(r"<tr.*?</tr>", m.group(0), _re.S | _re.I)]
        out.setdefault(k, rows)
    return out
def cepea_parse(rows, header_has):
    assert rows and header_has in _norm(" ".join(rows[0])), "encabezado/unidad Cepea inesperado: " + " ".join(rows[0])[:80]
    pts = []
    for r in rows[1:]:
        m = _re.search(r"(\d{2})/(\d{2})/(\d{4})\s*$", r[0])                       # "16 - 19/11/2026" (semana) o "19/11/2026" (dia): fecha = ultimo dia
        if not m: continue
        pts.append((f"{m.group(3)}-{m.group(2)}-{m.group(1)}", float(r[1].replace(".", "").replace(",", "."))))
    return pts
_CEPEA = {}
def c_cepea(page, key, header_has):
    def run():
        url = f"https://www.cepea.esalq.usp.br/br/indicador/{page}.aspx"
        if url not in _CEPEA: _CEPEA[url] = http(url, sid="cepea_" + page, timeout=60).decode("utf-8", "replace")
        raw = _CEPEA[url]
        assert "CC BY-NC 4.0" in raw, "la pagina ya no declara la licencia CC BY-NC 4.0: no se usa"
        pts = cepea_parse(cepea_tables(raw)[key], header_has)
        return {"pts": pts, "extra": {"licencia": "CEPEA (CC BY-NC 4.0): uso no comercial con atribucion; confirmar con Legal el uso corporativo", "pagina": url}}
    return run

_SOFRAI = {}
def c_sofr_avg(dias, term):
    """Promedios compuestos de SOFR de la NY Fed (30/90/180 dias, hacia atras). Una sola descarga por corrida para todas las series."""
    def run():
        if "rows" not in _SOFRAI:
            _SOFRAI["rows"] = jget("https://markets.newyorkfed.org/api/rates/secured/sofrai/last/500.json", sid="nyfed_sofrai")["refRates"]
        rows, campo = _SOFRAI["rows"], "average%dday" % dias
        assert all(r["type"] == "SOFRAI" for r in rows), "tipo de tasa inesperado"
        return {"pts": [(r["effectiveDate"], float(r[campo])) for r in rows if r.get(campo) is not None],
                "extra": {"nota": "promedio compuesto hacia atras de SOFR a %d dias; NO es CME Term SOFR %s (prospectivo)" % (dias, term)}}
    return run

# ---------- CZ App (Czarnikow): cierres ICE No. 11 y No. 5 ----------
# Informe diario publico, robots.txt de czapp.com permite /analyst-insights/ (Crawl-delay: 3); no se encontraron Terminos de Uso. 1 solicitud por dia habil, con
# validacion por cadena (cierre = cierre previo + variacion impresa). Se puede apagar con CZAPP_AUTO=0. La reutilizacion de datos ICE debe confirmarse con Czarnikow/ICE.
CZ_MON = {"jan": "ene", "feb": "feb", "mar": "mar", "apr": "abr", "may": "may", "jun": "jun", "jul": "jul", "aug": "ago", "sep": "sep", "oct": "oct", "nov": "nov", "dec": "dic"}
def _ord(n): return f"{n}{'th' if 11 <= n % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"
def cz_url(d): return f"https://www.czapp.com/analyst-insights/daily-market-price-updates-and-commentary-{_ord(d.day)}-{d.strftime('%B').lower()}-{d.year}/"
def cz_parse(raw):
    """HTML del informe -> {'sb11': {'mar-27': (cierre, variacion|None)}, 'sw5': {...}}; exige encabezados 'Settlement (cUS$/lb)' / 'Settlement (US$/mt)'."""
    out = {}
    for tb in _re.findall(r"<table.*?</table>", raw, _re.S | _re.I):
        rows = [[_cl(c) for c in _re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, _re.S | _re.I)] for r in _re.findall(r"<tr.*?</tr>", tb, _re.S | _re.I)]
        h = rows[0][0] if rows and rows[0] else ""
        key = "sb11" if h == "Settlement (cUS$/lb)" else ("sw5" if h == "Settlement (US$/mt)" else None)
        if not key: continue
        for r in rows[1:]:
            m = _re.fullmatch(r"([A-Z][a-z]{2})(\d{2})", r[0] if r else "")
            if not m or len(r) < 2: continue
            try: v = float(r[1].replace(",", ""))
            except ValueError: continue
            ch = None
            if len(r) > 2 and _re.fullmatch(r"[-+−]?\d+(\.\d+)?", r[2]):
                ch = float(r[2].replace("−", "-"))
                if len(r) > 3 and r[3] == "▼" and ch > 0: ch = -ch
            out.setdefault(key, {})[f"{CZ_MON[m.group(1).lower()]}-{m.group(2)}"] = (v, ch)
    return out
def czapp_refresh(srcs, res):
    """Anade a sb11_mar27 y sw5_mar27 los cierres de los dias habiles posteriores al ultimo guardado. Devuelve (fuentes_actualizadas, cierres) o None."""
    a, b = srcs.get("sb11_mar27"), srcs.get("sw5_mar27")
    if not a or not b or os.environ.get("CZAPP_AUTO") == "0": return None
    a, b = dict(a), dict(b); a["pts"], b["pts"] = [list(p) for p in a["pts"]], [list(p) for p in b["pts"]]
    d, cierres, got = dt.date.fromisoformat(a["pts"][-1][0]), None, []
    while True:
        d += dt.timedelta(days=1)
        if d > TODAY_GT: break
        if not is_bd(d, "US"): continue
        try: raw = http(cz_url(d), sid="czapp_" + d.isoformat(), timeout=60, retries=1).decode("utf-8", "replace")
        except RuntimeError as e:
            if "404" in str(e): LOG.append({"id": "czapp_" + d.isoformat(), "ok": False, "err": "informe aun no publicado (404)"}); continue
            raise
        time.sleep(3)                                                              # Crawl-delay: 3 de su robots.txt
        txt = _cl(raw); title = f"Daily Market Price Updates and Commentary {_ord(d.day)} {d.strftime('%B')} {d.year}"
        if title not in txt: raise RuntimeError(f"CZ App {d}: el titulo del informe no coincide con la fecha")
        t = cz_parse(raw)
        if "mar-27" not in t.get("sb11", {}) or "mar-27" not in t.get("sw5", {}): raise RuntimeError(f"CZ App {d}: no se encontro mar-27 con encabezado de settlement")
        for s, k in ((a, "sb11"), (b, "sw5")):
            v, ch = t[k]["mar-27"]; pv = s["pts"][-1][1]
            if ch is None or abs(pv + ch - v) > 0.0105: raise RuntimeError(f"CZ App {d} {k} mar-27: cadena rota ({pv} {ch} != {v}); no se publica")
            s["pts"].append([d.isoformat(), v])
        cierres = {"fecha": d.isoformat(), "fuente": "ICE vía CZ App (Czarnikow) · cierres (settlements)", "url": cz_url(d), "importado": res["generated"],
                   "metodo": "consulta automatica diaria del informe publico (1 por dia habil)",
                   "sb11": {c: {"v": v, "chg": ch} for c, (v, ch) in t["sb11"].items() if ch is not None}, "sw5": {c: {"v": v, "chg": ch} for c, (v, ch) in t["sw5"].items() if ch is not None}}
        got.append(d.isoformat())
    if not got: return None
    for s in (a, b):
        s["fetched"] = res["generated"]; s["url"] = cierres["url"]; s["ultima_fecha"] = s["pts"][-1][0]
        s["extra"] = dict(s.get("extra") or {}, metodo="consulta automatica diaria de informes publicos de CZ App (historial inicial verificado e importado); cadena de variaciones validada", preservar=True)
        s.update(status_of({m: s.get(m) for m in ("freq", "cal", "lag", "tol")}, [tuple(x) for x in s["pts"]]))
    res["changes"].append({"id": "czapp", "fecha_anterior": srcs["sb11_mar27"]["pts"][-1][0], "valor_anterior": srcs["sb11_mar27"]["pts"][-1][1], "fecha_nueva": a["pts"][-1][0], "valor_nuevo": a["pts"][-1][1], "estado": a["estado"], "avisos": ["dias nuevos: " + ", ".join(got)]})
    return {"sb11_mar27": a, "sw5_mar27": b}, cierres

# ---------- registro de indicadores ----------
# ---------- POLITICA DE PUBLICACION (modo publico: PUBLIC_MODE=1) ----------
# El live.json que se publica en el repositorio publico SOLO contiene series cuya licencia permite redistribuirlas, con su atribucion.
# Nunca incluye: analisis interno de Magdalena, datos FMP (planes personales), cierres ICE de CZ App (sin permiso confirmado). Cepea (CC BY-NC) se publica con atribucion y se puede retirar con ALLOW_NC=0.
PUBLISH = {
 "sofr_on": ("NY Fed", "Datos publicos del Federal Reserve Bank of New York (fuente: NY Fed Markets Data)"), "effr": ("NY Fed", "Datos publicos del NY Fed"), "sofr_avg90": ("NY Fed", "Datos publicos del NY Fed"),
 "sofr_avg180": ("NY Fed", "Datos publicos del NY Fed"),
 "brl_bcb": ("BCB", "Banco Central do Brasil, datos abiertos (PTAX)"), "gtq_banguat": ("Banguat", "Banco de Guatemala, tipo de cambio de referencia (informacion publica)"),
 "brent_eia": ("EIA", "U.S. Energy Information Administration (dominio publico; citar la fuente)"), "wti_eia": ("EIA", "U.S. Energy Information Administration (dominio publico; citar la fuente)"),
 "inr_ecb": ("BCE", "Banco Central Europeo, tipos de referencia (reutilizacion con atribucion)"), "etanol_anp": ("ANP", "ANP Brasil, datos abiertos (Levantamento de Precos de Combustiveis)"),
 "wb_sugar_world": ("Banco Mundial", "World Bank Commodity Price Data (Pink Sheet), terminos de uso de datasets del Banco Mundial (CC BY 4.0)"), "wb_sugar_us": ("Banco Mundial", "World Bank Commodity Price Data (Pink Sheet), CC BY 4.0"),
 "hid_cepea": ("CEPEA/ESALQ-USP", "CC BY-NC 4.0: uso no comercial con atribucion (Fonte: CEPEA/ESALQ-USP)"), "anidro_cepea": ("CEPEA/ESALQ-USP", "CC BY-NC 4.0"), "acucar_sp_cepea": ("CEPEA/ESALQ-USP", "CC BY-NC 4.0"), "acucar_santos_cepea": ("CEPEA/ESALQ-USP", "CC BY-NC 4.0")}
NC_IDS = {"hid_cepea", "anidro_cepea", "acucar_sp_cepea", "acucar_santos_cepea"}
PUBLIC = os.environ.get("PUBLIC_MODE") == "1"
OPTIONAL = {"etanol_anp"}          # fuentes que bloquean las IP de servidores en la nube (gov.br: "Connection reset"): se conserva el ultimo dato y se avisa, sin fallar el job
def publishable(sid): return sid in PUBLISH and (os.environ.get("ALLOW_NC", "1") != "0" or sid not in NC_IDS)
ALERT_AFTER = {"brent_eia": 7, "wti_eia": 7}    # dias habiles de atraso que se toleran antes de alertar. EIA publica por semana: margen mayor
def stale_fails(sid, s):
    """Obsoleto que debe fallar el job (nivel 'estancado'). Diarias: tol + 3 dias habiles; semanales y mensuales: tol. Opcionales y privadas solo avisan."""
    if sid in OPTIONAL or not publishable(sid): return False
    tol = s.get("tol", 1)
    return s.get("atraso", 0) > ALERT_AFTER.get(sid, tol + 3 if s.get("freq") == "d" else tol)

def M(**k): return k
DEFS = [
 ("sofr_on", M(label="SOFR diario (NY Fed)", unit="%", dec=2, freq="d", kind="cierre", cal="US", lag=1, tol=1, rng=(0, 10), provider="NY Fed",
               src="NY Fed · Secured Overnight Financing Rate (cierre oficial; NO es Term SOFR)", url="https://www.newyorkfed.org/markets/reference-rates/sofr",
               definition="SOFR overnight, tasa efectiva del dia; publicada ~8:00 ET del siguiente dia habil"), c_nyfed("secured/sofr")),
 ("effr", M(label="Fed funds efectiva (NY Fed)", unit="%", dec=2, freq="d", kind="cierre", cal="US", lag=1, tol=1, frozen_n=9999, rng=(0, 10), provider="NY Fed",
            src="NY Fed · Effective Federal Funds Rate (cierre oficial)", url="https://www.newyorkfed.org/markets/reference-rates/effr",
            definition="Tasa efectiva de fondos federales (overnight)"), c_nyfed("unsecured/effr")),
 ("brl_bcb", M(label="USD/BRL (BCB PTAX)", unit="BRL por USD", dec=4, freq="d", kind="cierre", cal="BR", lag=1, tol=1, rng=(2, 12), provider="Banco Central do Brasil",
               src="Banco Central do Brasil · PTAX venta, boletin de cierre (API Olinda)", url="https://www.bcb.gov.br/estabilidadefinanceira/historicocotacoes",
               definition="Dolar comercial, tasa PTAX de venta del boletin de cierre (~13:00 hora de Brasilia)"), c_bcb_ptax),
 ("gtq_banguat", M(label="USD/GTQ (Banguat)", unit="GTQ por USD", dec=5, freq="d", kind="cierre", cal="GT", lag=0, tol=1, rng=(6, 9), provider="Banco de Guatemala",
                   src="Banco de Guatemala · tipo de cambio de referencia (servicio web)", url="https://www.banguat.gob.gt/tipo_cambio/",
                   definition="Tipo de cambio de referencia del quetzal frente al dolar, publicado por adelantado para el siguiente dia habil"), c_banguat),
 ("brent_eia", M(label="Brent spot FOB Europa (EIA)", unit="USD/bbl", dec=2, freq="d", kind="cierre", cal="US", lag=3, tol=2, rng=(10, 250), provider="EIA",
                 src="EIA · Europe Brent Spot Price FOB (RBRTE), diario con rezago (API v2 o archivo masivo oficial)", url="https://www.eia.gov/dnav/pet/hist/RBRTED.htm",
                 definition="Brent spot FOB; NO es el contrato ICE de 1er mes del radar.json"), c_eia("RBRTE")),
 ("wti_eia", M(label="WTI Cushing spot (EIA)", unit="USD/bbl", dec=2, freq="d", kind="cierre", cal="US", lag=3, tol=2, rng=(10, 250), provider="EIA",
               src="EIA · Cushing OK WTI Spot Price FOB (RWTC), diario con rezago (API v2 o archivo masivo oficial)", url="https://www.eia.gov/dnav/pet/hist/RWTCD.htm",
               definition="WTI spot FOB Cushing; NO es el futuro NYMEX"), c_eia("RWTC")),
 ("inr_ecb", M(label="USD/INR (cruce BCE)", unit="INR por USD", dec=4, freq="d", kind="ref", cal="EU", lag=1, tol=1, rng=(40, 150), provider="Banco Central Europeo",
               src="BCE · referencia EUR/INR ÷ EUR/USD (derivado, ~14:15 CET)", url="https://www.ecb.europa.eu/stats/policy_and_exchange_rates/euro_reference_exchange_rates/",
               definition="Cruce derivado de las referencias del BCE; NO es el cierre de mercado USD/INR del radar.json"), c_ecb_inr),
 ("brent_fmp", M(label="Brent 1er mes (FMP, EOD ICE)", unit="USD/bbl", dec=2, freq="d", kind="cierre", cal="UK", lag=0, tol=1, rng=(10, 250), kpi="brent", provider="FMP",
                 src="FMP · BZUSD, cierre EOD del contrato ICE Brent de 1er mes (misma fuente y serie que el indicador Brent de la edicion)", url="https://site.financialmodelingprep.com/developer/docs/stable/commodities-historical-price-eod-light",
                 definition="Brent ICE 1er mes, cierre diario. Verificado: el cierre FMP del 11 nov es identico al KPI brent del radar.json"), c_fmp("BZUSD")),
 ("inr_fmp", M(label="USD/INR (FMP, EOD)", unit="INR por USD", dec=3, freq="d", kind="cierre", cal="US", lag=0, tol=1, rng=(40, 150), kpi="inr", provider="FMP",
               src="FMP · USDINR, cierre EOD (misma fuente y serie que el indicador USD/INR de la edicion)", url="https://site.financialmodelingprep.com/developer/docs/stable/forex-historical-price-eod-light",
               definition="USD/INR spot, cierre diario. Verificado: el cierre FMP del 11 nov es identico al KPI inr del radar.json"), c_fmp("USDINR")),
 ("hid_cepea", M(label="Etanol hidratado combustible (Cepea/Esalq SP)", unit="R$/l", dec=4, freq="w", kind="semanal", cal="BR", lag=0, tol=9, rng=(0.5, 15), kpi="hid", provider="CEPEA/ESALQ-USP",
                 src="CEPEA/ESALQ-USP · Indicador semanal del etanol hidratado combustible, Sao Paulo (CC BY-NC 4.0)", url="https://www.cepea.esalq.usp.br/br/indicador/etanol.aspx",
                 definition="MISMA serie que el KPI hid del radar.json (2.8799 R$/l, semana 2-6 nov 2026: verificado). Sin flete ni ICMS; PIS/Cofins zerado"), c_cepea("etanol", "HIDRATADO COMBUSTIVEL", "R$/LITRO")),
 ("anidro_cepea", M(label="Etanol anhidro (Cepea/Esalq SP)", unit="R$/l", dec=4, freq="w", kind="semanal", cal="BR", lag=0, tol=9, rng=(0.5, 15), provider="CEPEA/ESALQ-USP",
                    src="CEPEA/ESALQ-USP · Indicador semanal del etanol anhidro, Sao Paulo (CC BY-NC 4.0)", url="https://www.cepea.esalq.usp.br/br/indicador/etanol.aspx",
                    definition="Etanol anhidro semanal, sin flete y sin PIS/Cofins; complementa al hidratado"), c_cepea("etanol", "ANIDRO", "R$/LITRO")),
 ("acucar_sp_cepea", M(label="Azucar cristal blanco SP (Cepea/Esalq)", unit="R$/saca 50 kg", dec=2, freq="d", kind="cierre", cal="BR", lag=0, tol=1, rng=(20, 600), provider="CEPEA/ESALQ-USP",
                       src="CEPEA/ESALQ-USP · Indicador diario del azucar cristal blanco, Sao Paulo (CC BY-NC 4.0)", url="https://www.cepea.esalq.usp.br/br/indicador/acucar.aspx",
                       definition="Azucar fisico en Brasil (R$/saca 50 kg); NO es el contrato No. 11 de ICE (c/lb) ni el No. 5"), c_cepea("acucar", "CRISTAL BRANCO", "VALOR R$")),
 ("acucar_santos_cepea", M(label="Azucar cristal Santos FOB (Cepea/Esalq)", unit="R$/saca 50 kg", dec=2, freq="d", kind="cierre", cal="BR", lag=0, tol=1, rng=(20, 600), provider="CEPEA/ESALQ-USP",
                          src="CEPEA/ESALQ-USP · Indicador del azucar cristal, Santos (FOB) (CC BY-NC 4.0)", url="https://www.cepea.esalq.usp.br/br/indicador/acucar.aspx",
                          definition="Azucar fisico FOB Santos (R$/saca 50 kg); NO es el contrato No. 11 de ICE"), c_cepea("acucar", "CRISTAL - SANTOS", "VALOR R$")),
 ("sofr_avg90", M(label="SOFR promedio 90 días (ref. Term 3M)", unit="%", dec=3, freq="d", kind="cierre", cal="US", lag=1, tol=1, rng=(0, 10), provider="NY Fed",
                  src="NY Fed · SOFR Averages (90 dias, hacia atras)", url="https://www.newyorkfed.org/markets/reference-rates/sofr-averages-and-index",
                  definition="Promedio compuesto de SOFR a 90 dias, hacia atras; referencia libre para Term SOFR 3M, pero NO es CME Term SOFR 3M (prospectivo, con licencia CME)"), c_sofr_avg(90, "3M")),
 ("sofr_avg180", M(label="SOFR promedio 180 días (ref. Term 6M)", unit="%", dec=3, freq="d", kind="cierre", cal="US", lag=1, tol=1, rng=(0, 10), provider="NY Fed",
                   src="NY Fed · SOFR Averages (180 dias, hacia atras)", url="https://www.newyorkfed.org/markets/reference-rates/sofr-averages-and-index",
                   definition="Promedio compuesto de SOFR a 180 dias, hacia atras; referencia libre para Term SOFR 6M, pero NO es CME Term SOFR 6M (prospectivo, con licencia CME)"), c_sofr_avg(180, "6M")),
 ("etanol_anp", M(label="Etanol hidratado al consumidor (ANP)", unit="R$/l", dec=3, freq="w", kind="semanal", cal="BR", lag=0, tol=9, rng=(1, 15), provider="ANP Brasil",
                  src="ANP · Levantamiento de Precios de Combustibles, producto ETANOL (promedio simple semanal de bombas)", url="https://www.gov.br/anp/pt-br/centrais-de-conteudo/dados-abertos/serie-historica-de-precos-de-combustiveis",
                  definition="Precio al consumidor (bomba), NO el precio de productor Cepea/Esalq del radar.json"), c_anp),
]
WB_DEFS = {
 "wb_sugar_world": M(label="Azúcar mundial (Banco Mundial, prom. mensual)", unit="USD/kg", dec=3, freq="m", kind="mensual", cal="US", lag=0, tol=35, rng=(0.001, 5), keep=300, provider="Banco Mundial",
                     src="Banco Mundial · Commodity Price Data (Pink Sheet), Sugar, world (ISA)", url="https://www.worldbank.org/en/research/commodity-markets",
                     definition="Promedio mensual del precio ISA diario, azucar crudo FOB Caribe; NO es el No. 11 ICE de cierre diario"),
 "wb_sugar_us": M(label="Azúcar EE. UU. (Banco Mundial, prom. mensual)", unit="USD/kg", dec=3, freq="m", kind="mensual", cal="US", lag=0, tol=35, rng=(0.001, 5), keep=300, provider="Banco Mundial",
                  src="Banco Mundial · Commodity Price Data (Pink Sheet), Sugar, US (referencia complementaria del contrato No. 16)", url="https://www.worldbank.org/en/research/commodity-markets",
                  definition="Promedio mensual, precio de importacion EE. UU. (contrato No. 16); complemento mensual, no sustituye al No. 11"),
}

def validate(sid, meta, pts):
    """Devuelve (pts_limpios, avisos)."""
    warns = []
    seen, out = set(), []
    for d, v in pts:
        try: dd = dt.date.fromisoformat(d)
        except ValueError: warns.append(f"fecha invalida {d}"); continue
        if dd > TODAY_GT: warns.append(f"fecha futura {d} descartada"); continue
        if not (meta["rng"][0] <= v <= meta["rng"][1]): warns.append(f"valor fuera de rango {d}={v}"); continue
        if d in seen: continue
        seen.add(d); out.append((d, v))
    out.sort()
    # congelado: >=5 observaciones HABILES consecutivas identicas (en series diarias)
    if meta["freq"] == "d" and len(out) >= 5:
        n = meta.get("frozen_n", 5)
        tail = [v for d, v in out[-(n + 4):] if is_bd(dt.date.fromisoformat(d), meta["cal"])][-n:]
        if len(tail) == n and len(set(tail)) == 1: warns.append(f"posible dato congelado ({n} dias habiles identicos)")
    return out, warns

def status_of(meta, pts):
    if not pts: return {"estado": "sin_datos"}
    last = dt.date.fromisoformat(pts[-1][0])
    if meta["freq"] == "m": exp = TODAY_GT.replace(day=1) - dt.timedelta(days=1); exp = exp.replace(day=1)   # mes anterior
    elif meta["freq"] == "w": exp = TODAY_GT - dt.timedelta(days=7)
    else: exp = expected_last(TODAY_GT, meta["cal"], meta["lag"])
    late = bd_between(last, exp, meta["cal"]) if meta["freq"] == "d" else (exp - last).days
    stale = late > meta["tol"]
    return {"estado": "obsoleto" if stale else "ultimo_cierre", "fecha_esperada": exp.isoformat(), "atraso": late}

def ethanol_reference(final):
    """Referencia de etanol hidratado de Brasil con la fuente usada y la fecha del dato.
    ANP (precio al consumidor) si esta vigente y se descargo en esta corrida; si no, Cepea/Esalq (precio de productor SP, otro concepto de precio) como respaldo.
    Cada serie conserva su propio historial: el respaldo NO se mezcla en etanol_anp; solo se etiqueta cual se uso."""
    def pick(sid):
        s = final.get(sid)
        return s if s and s.get("pts") else None
    anp, cep = pick("etanol_anp"), pick("hid_cepea")
    anp_ok = anp and not anp.get("fallback") and anp.get("estado") != "obsoleto"
    used, why = (anp, None) if anp_ok else (cep, None)
    if not anp_ok:
        why = ("ANP sin datos nuevos en esta corrida (" + str(anp.get("error_ultima_corrida", "dato obsoleto"))[:100] + ")") if anp else "ANP sin datos guardados"
        if not cep: used = anp                                         # sin respaldo disponible: se informa lo ultimo de ANP, marcado como no vigente
    if not used: return None
    sid = used["id"]
    out = {"fuente_usada": "anp" if sid == "etanol_anp" else "cepea", "serie_id": sid, "proveedor": used.get("provider"), "etiqueta": used.get("label"),
           "fecha_dato": used["pts"][-1][0], "valor": used["pts"][-1][1], "unidad": used.get("unit"), "estado": used.get("estado"),
           "tipo_precio": "consumidor (bomba)" if sid == "etanol_anp" else "productor Sao Paulo (sin flete ni ICMS)", "generado": NOW.strftime("%Y-%m-%dT%H:%M:%SZ")}
    if why: out["motivo_respaldo"] = why
    if anp: out["anp_ultima_fecha"] = anp["pts"][-1][0]
    return out

def main(out_path, only=None, keep_log=True):
    prev = {}
    if os.path.exists(out_path):
        try: prev = json.load(open(out_path, encoding="utf-8"))
        except Exception: prev = {}
    res = {"schema": 2, "generated": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), "tz": "America/Guatemala (UTC-6)", "sources": {}, "errors": {}, "changes": []}
    jobs = list(DEFS)
    wb_done = None
    todo = [(sid, meta, fn) for sid, meta, fn in jobs if (not only or sid in only) and (not PUBLIC or publishable(sid))]
    for sid, meta, fn in todo:
        try:
            r = fn(); r = r if isinstance(r, dict) else {"pts": r}
            res["sources"][sid] = (meta, r)
        except Exception as e:
            res["errors"][sid] = str(e)[:160]; print(f"ERR {sid}: {e}")
    if (not only or any(k in only for k in WB_DEFS)) and (not PUBLIC or publishable("wb_sugar_world")):
        try:
            wb = c_worldbank([("Sugar, world", "wb_sugar_world"), ("Sugar, US", "wb_sugar_us")])
            for k, r in wb.items():
                if not only or k in only: res["sources"][k] = (WB_DEFS[k], r)
            for k in WB_DEFS:
                if k not in wb: res["errors"][k] = "fila no encontrada en el Pink Sheet"
        except Exception as e:
            for k in WB_DEFS: res["errors"][k] = str(e)[:160]
            print("ERR worldbank:", e)
    if os.environ.get("FRED_API_KEY"):
        pass   # los conectores FRED (c_fred) se activan aqui cuando exista la clave; sin clave se omite
    final = {}
    for sid, (meta, r) in res["sources"].items():
        pts, warns = validate(sid, meta, r["pts"])
        old = (prev.get("sources") or {}).get(sid, {})
        merged = {d: v for d, v in (old.get("pts") or [])}
        merged.update(dict(pts))                                   # nuevo manda; historial previo se conserva
        pts_m = sorted(merged.items())[-meta.get("keep", 1500):]
        if len(pts_m) < 2:
            res["errors"][sid] = "menos de 2 observaciones validas"; continue
        s = {k: v for k, v in meta.items() if k not in ("rng", "frozen_n", "keep")}
        s.update({"id": sid, "pts": [[d, v] for d, v in pts_m], "fetched": res["generated"], "avisos": warns,
                  "asof_time": r.get("asof_time"), "extra": r.get("extra")})
        s.update(status_of(meta, pts_m))
        s["ultima_fecha"] = pts_m[-1][0]
        final[sid] = s
        po = (old.get("pts") or [[None, None]])[-1]
        res["changes"].append({"id": sid, "fecha_anterior": po[0], "valor_anterior": po[1], "fecha_nueva": pts_m[-1][0], "valor_nuevo": pts_m[-1][1],
                               "estado": s["estado"], "avisos": warns})
    for k, v in (prev.get("sources") or {}).items():            # fuente que fallo hoy: se conserva el ultimo dato valido guardado y se registra el error
        if k not in final and k in res["errors"] and v.get("pts"):
            v = dict(v); v["error_ultima_corrida"] = res["errors"][k]; v["fallback"] = True
            v.update(status_of({m: v.get(m) for m in ("freq", "cal", "lag", "tol")}, [tuple(x) for x in v["pts"]])); final[k] = v
    defids = {sid for sid, _, _ in DEFS} | set(WB_DEFS)                # series importadas manualmente (p. ej. cierres CZ App): se conservan y su estado se recalcula con la fecha de hoy
    for k, v in (prev.get("sources") or {}).items():
        if k not in final and k not in defids and ((v.get("extra") or {}).get("preservar") or "importacion manual" in ((v.get("extra") or {}).get("metodo") or "")) and v.get("pts"):
            v = dict(v); v.update(status_of({m: v.get(m) for m in ("freq", "cal", "lag", "tol")}, [tuple(x) for x in v["pts"]])); final[k] = v
    if prev.get("cierres"): res["cierres"] = prev["cierres"]
    try:
        cz = None if PUBLIC else czapp_refresh(final, res)                      # CZ App NO se publica hasta confirmar permisos de uso corporativo
        if cz: final.update(cz[0]); res["cierres"] = cz[1]
    except Exception as e:
        res["errors"]["czapp"] = str(e)[:160]; print("ERR czapp:", e)
    if only:                                                       # con --only se conservan las demas series tal como estaban
        for k, v in (prev.get("sources") or {}).items(): final.setdefault(k, v)
    if PUBLIC:                                                          # modo publico: solo series con redistribucion permitida; nada manual, nada de CZ App/FMP
        final = {k: v for k, v in final.items() if publishable(k)}; res.pop("cierres", None)
        res["publico"] = True; res["aviso"] = "Datos de mercado publicos con atribucion. No incluye analisis interno ni datos de proveedores sin permiso de redistribucion."
        res["licencias"] = {k: {"proveedor": PUBLISH[k][0], "licencia": PUBLISH[k][1]} for k in final}
    res["sources"] = final
    eth = ethanol_reference(final)                                     # etanol hidratado de Brasil: ANP si esta vigente; si no, Cepea/Esalq (sin mezclar las series)
    if eth: res["etanol_hidratado_br"] = eth
    alertas = []                                                  # alertas: fuentes caidas, datos conservados, congelados y obsoletos
    for sid, err in res["errors"].items():
        k = "conservado" if sid in final and final[sid].get("fallback") else "sin_dato"
        alertas.append({"id": sid, "nivel": "aviso" if (sid in OPTIONAL and k == "conservado") else ("error" if k == "sin_dato" else "caida"), "msg": f"fuente caida ({err}); " + ("se conserva el ultimo dato valido del " + (final[sid].get("ultima_fecha") or final[sid]["pts"][-1][0]) if k == "conservado" else "no hay dato guardado")})
    for sid, s in final.items():
        if any("congelado" in w for w in s.get("avisos") or []): alertas.append({"id": sid, "nivel": "congelado", "msg": "posible dato congelado"})
        if s.get("estado") == "obsoleto":
            alertas.append({"id": sid, "nivel": "estancado" if stale_fails(sid, s) else "obsoleto",
                            "msg": f"excede su frecuencia: esperado {s.get('fecha_esperada')}, ultimo {s['pts'][-1][0]} ({s.get('atraso')} {'dias habiles' if s.get('freq') == 'd' else 'dias'})"})
    res["alertas"] = alertas
    res["log"] = LOG if keep_log else []
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    hd = os.environ.get("HISTORY_DIR")                                  # historicos auxiliares (date,value) para graficos 1M/3M/6M/1A/MAX y auditoria
    if hd:
        os.makedirs(hd, exist_ok=True)
        for k, v in final.items():
            with open(os.path.join(hd, k + ".csv"), "w", encoding="utf-8", newline="\n") as f:
                f.write("date,value\n" + "".join(f"{d},{x}\n" for d, x in v["pts"]))
    with open(out_path, "w", encoding="utf-8", newline="\n") as f: json.dump(res, f, ensure_ascii=False, separators=(",", ":"))
    print(f"\n{'indicador':16}{'anterior':>12}{'nuevo':>12}  {'valor':>10}  estado")
    for c in res["changes"]:
        print(f"{c['id']:16}{str(c['fecha_anterior']):>12}{c['fecha_nueva']:>12}  {c['valor_nuevo']:>10}  {c['estado']}" + (f"  AVISOS: {c['avisos']}" if c["avisos"] else ""))
    for k, v in res["errors"].items(): print(f"ERROR {k}: {v}")
    for a in alertas: print(f"ALERTA[{a['nivel']}] {a['id']}: {a['msg']}")
    print("->", out_path, os.path.getsize(out_path), "bytes")
    return 0 if final else 1

def alert_code(path):
    """codigo 3 si hay fuentes caidas, congeladas o estancadas (GitHub Actions avisa por correo); 'obsoleto' (atraso corto, opcional o privada) es informativo."""
    return 3 if any(a["nivel"] in ("error", "caida", "congelado", "estancado") for a in json.load(open(path, encoding="utf-8")).get("alertas", [])) else 0

if __name__ == "__main__":
    a = [x for x in sys.argv[1:] if not x.startswith("--")]
    only = None
    for x in sys.argv[1:]:
        if x.startswith("--only="): only = set(x[7:].split(","))
    out = a[0] if a else os.path.join(os.path.dirname(__file__), "..", "data", "live.json")
    rc = main(out, only, "--no-log" not in sys.argv)
    sys.exit(rc or (alert_code(out) if "--fail-on-alert" in sys.argv else 0))
