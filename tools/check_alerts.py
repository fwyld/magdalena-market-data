#!/usr/bin/env python3
"""Lee data/live.json y reporta alertas como anotaciones de GitHub Actions.

- Una fuente caida, congelada o estancada es solo ::warning:: (el dato ya se publico con el ultimo valor valido).
- El job falla unicamente si MAS DE LA MITAD de las series estan caidas (error/caida): señal de problema de red o de codigo, no de una fuente.
  La validacion del JSON es un paso aparte y falla por su cuenta.
Uso: python tools/check_alerts.py [data/live.json]
"""
import json, sys

path = sys.argv[1] if len(sys.argv) > 1 else "data/live.json"
d = json.load(open(path, encoding="utf-8"))
alertas = d.get("alertas", [])
ids = set(d.get("sources", {})) | set(d.get("errors", {}))
down = {a["id"] for a in alertas if a["nivel"] in ("error", "caida")}

for a in alertas:
    print(f"::warning title=Fuente {a['id']} ({a['nivel']})::{a['msg']}")
eth = d.get("etanol_hidratado_br")
if eth:
    print(f"Etanol hidratado BR: fuente={eth['fuente_usada']} fecha_dato={eth['fecha_dato']} valor={eth['valor']} {eth.get('unidad','')}")

if ids and len(down) * 2 > len(ids):
    print(f"::error::{len(down)} de {len(ids)} fuentes caidas (mayoria): {', '.join(sorted(down))}")
    sys.exit(1)
print(f"OK: {len(down)} caidas de {len(ids)} series, {len(alertas)} alertas (solo avisos)")
