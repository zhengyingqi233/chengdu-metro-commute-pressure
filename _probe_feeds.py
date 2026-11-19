import json
import requests

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://map.amap.com/subway/index.html",
}
url = "https://map.amap.com/service/subway?srhdata=5101_drw_chengdu.json"
data = requests.get(url, headers=headers, timeout=30).json()
print("top keys", data.keys())
print("city", data.get("s"), data.get("i"), "lines", len(data["l"]))
line = data["l"][0]
print("line keys", line.keys())
print({k: line[k] for k in line if k != "st"})
st = line["st"][0]
print("station keys", st.keys())
print(json.dumps(st, ensure_ascii=False)[:800])
print("--- lines ---")
for ln in data["l"]:
    print(ln.get("ls"), ln.get("ln"), ln.get("kn"), ln.get("la"), "stops", len(ln.get("st", [])), "cl", ln.get("cl"))
    sample = ln["st"][0]
    print("  sl", sample.get("sl"), "n", sample.get("n"))
