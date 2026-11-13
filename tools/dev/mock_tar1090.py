#!/usr/bin/env python3
"""Synthetic tar1090 feed for local UI work and E2E runs. Stdlib only.

Serves ``/data/aircraft.json`` and ``/data/receiver.json`` on 127.0.0.1 with a few hundred
plausible aircraft circulating around a placeholder receiver in central London
(51.50, -0.10 — the same stand-in the README screenshots use). The fleet is seeded, so
runs are repeatable; there is no real traffic and no real location in it.

    python tools/dev/mock_tar1090.py [port=8771] [aircraft=340]

Pair it with ``tools/dev/run_dev.py``, which starts PiScope against this feed.

The first three aircraft are a fixed "cast" so screenshots and tests have known targets:
an emergency squawk (hex 4078a1), a close-in police helicopter (43c8f9) and a
transatlantic widebody (407c2e, callsign BAW117).
"""
from __future__ import annotations

import json
import math
import random
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

RX_LAT, RX_LON = 51.50, -0.10
T0 = time.time()
rng = random.Random(595)

# (callsign prefix, registration prefix, hex block, type designators)
AIRLINES = [
    ("BAW", "G-", 0x400000, ["A320", "A319", "A20N", "A21N", "B772", "B789", "A35K", "B77W", "A388"]),
    ("EZY", "G-", 0x406000, ["A319", "A320", "A20N", "A21N"]),
    ("RYR", "EI-", 0x4CA000, ["B738", "B38M"]),
    ("DLH", "D-", 0x3C4000, ["A320", "A20N", "A21N", "A359", "B748"]),
    ("AFR", "F-", 0x392000, ["A320", "A318", "A359", "B77W", "A223"]),
    ("KLM", "PH-", 0x484000, ["B738", "E190", "E75L", "B789", "A21N"]),
    ("UAE", "A6-", 0x896000, ["A388", "B77W"]),
    ("QTR", "A7-", 0x06A000, ["A35K", "B77W", "B788"]),
    ("VIR", "G-", 0x407000, ["A35K", "B789", "A339"]),
    ("EXS", "G-", 0x408000, ["B738", "A21N"]),
    ("WZZ", "HA-", 0x471000, ["A21N", "A320"]),
    ("AAL", "N", 0xA00000, ["B772", "B77W", "B788"]),
    ("UAL", "N", 0xA40000, ["B763", "B772", "B78X"]),
    ("DAL", "N", 0xA80000, ["A333", "B764", "A339"]),
    ("SHT", "G-", 0x409000, ["A320", "A319"]),
    ("TOM", "G-", 0x40A000, ["B738", "B38M", "B788"]),
    ("SAS", "SE-", 0x4A8000, ["A20N", "A320", "CRJ9"]),
    ("IBE", "EC-", 0x344000, ["A20N", "A21N", "A359"]),
    ("THY", "TC-", 0x4BA000, ["A21N", "A333", "B77W", "A359"]),
    ("LOG", "G-", 0x40B000, ["AT76", "E145"]),
]
HEAVY = {"B772", "B77W", "B789", "B788", "B78X", "A35K", "A359", "A388", "A333", "A339", "B748", "B763", "B764"}
AIRPORTS = [("LHR", 51.4700, -0.4543), ("LGW", 51.1537, -0.1821), ("STN", 51.8850, 0.2350),
            ("LTN", 51.8747, -0.3683), ("LCY", 51.5053, 0.0553)]
LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def dest_point(lat: float, lon: float, bearing_deg: float, dist_nm: float) -> tuple[float, float]:
    r = 3440.065
    b = math.radians(bearing_deg)
    p1, l1 = math.radians(lat), math.radians(lon)
    d = dist_nm / r
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(b))
    l2 = l1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(p1), math.cos(d) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), (math.degrees(l2) + 540) % 360 - 180


def _reg(prefix: str) -> str:
    if prefix == "N":
        return f"N{rng.randrange(100, 999)}{rng.choice('ABDU')}{rng.choice('ALNW')}"
    return prefix + "".join(rng.choice(LETTERS) for _ in range(4))


def make_fleet(n: int) -> list[dict]:
    fleet: list[dict] = []
    used: set[int] = set()

    def new_hex(base: int) -> str:
        while True:
            h = base + rng.randrange(0, 0xFFF)
            if h not in used:
                used.add(h)
                return f"{h:06x}"

    for _ in range(n):
        roll = rng.random()
        ac: dict = {"phase": rng.random() * 1000}
        if roll < 0.74:                                    # airliners
            pfx, regp, base, types = rng.choice(AIRLINES)
            t = rng.choice(types)
            suffix = rng.choice(["", "", rng.choice("ABCDEFGHJKLMNPRSTUVWXYZ"), rng.choice("ABCDEFGH") + rng.choice("KLMNPRST")])
            num = rng.randrange(1, 9999) % (1000 if suffix else 10000)
            ac.update(flight=f"{pfx}{num}{suffix}"[:8], t=t, r=_reg(regp), hex=new_hex(base),
                      category="A5" if t in HEAVY else "A3")
            kind = rng.random()
            if kind < 0.55:      # cruise
                ac.update(alt=rng.choice([30000, 32000, 34000, 35000, 36000, 37000, 38000, 39000, 40000, 41000]),
                          gs=rng.uniform(410, 520), rate=0, dist=rng.uniform(25, 215))
            elif kind < 0.80:    # descending / approach
                ac.update(alt=rng.randrange(6000, 24000, 25), gs=rng.uniform(160, 330),
                          rate=-rng.randrange(600, 1800, 64), dist=rng.uniform(4, 95))
            else:                # climbing out
                ac.update(alt=rng.randrange(3000, 20000, 25), gs=rng.uniform(220, 420),
                          rate=rng.randrange(900, 2400, 64), dist=rng.uniform(4, 110))
        elif roll < 0.84:                                  # general aviation
            reg = _reg("G-")
            ac.update(flight=reg.replace("-", ""), r=reg,
                      t=rng.choice(["C172", "PA28", "DA40", "SR22", "C152", "P28A", "DA42", "TBM9", "PC12"]),
                      hex=new_hex(0x401000), category="A1", alt=rng.randrange(900, 6500, 25),
                      gs=rng.uniform(85, 190), rate=rng.choice([0, 0, 0, 300, -300]), dist=rng.uniform(3, 120))
        elif roll < 0.89:                                  # helicopters
            ac.update(flight=rng.choice(["HLE", "NPAS", "UKP", "REDHED"]) + str(rng.randrange(1, 99)), r=_reg("G-"),
                      t=rng.choice(["EC35", "EC45", "A139", "A109", "S92", "H145"]), hex=new_hex(0x402000),
                      category="A7", alt=rng.randrange(400, 2400, 25), gs=rng.uniform(20, 135),
                      rate=rng.choice([0, 0, 200, -200]), dist=rng.uniform(1, 70))
        elif roll < 0.93:                                  # military
            t = rng.choice(["A400", "C17", "EUFI", "K35R", "A332", "C30J", "F35", "H47"])
            ac.update(flight=(rng.choice(["RRR", "ASCOT", "TARTN", "NATO", "RCH", "LAGR"]) + str(rng.randrange(10, 9999)))[:8],
                      r=f"ZM{rng.randrange(100, 999)}", t=t, hex=new_hex(0x43C000), mil=True,
                      category="A7" if t == "H47" else ("A6" if t in ("EUFI", "F35") else "A5"),
                      alt=rng.randrange(1500, 31000, 100), gs=rng.uniform(120, 460),
                      rate=rng.choice([0, 0, 1200, -900]), dist=rng.uniform(8, 190))
        elif roll < 0.96:                                  # business jets
            ac.update(flight=rng.choice(["NJE", "VJT", "GAC", "LXJ"]) + str(rng.randrange(1, 999)) + rng.choice("ABCDKLM"),
                      r=_reg(rng.choice(["G-", "M-", "9H-", "OE-", "CS-"])),
                      t=rng.choice(["C56X", "GLF6", "E55P", "CL60", "F2TH", "GL7T", "C68A"]),
                      hex=new_hex(0x4D2000), category="A2", alt=rng.randrange(6000, 45000, 100),
                      gs=rng.uniform(250, 480), rate=rng.choice([0, 0, 1800, -1400]), dist=rng.uniform(6, 200))
        else:                                              # taxiing at an airport
            pfx, regp, base, types = rng.choice(AIRLINES[:6])
            ap = rng.choice(AIRPORTS)
            ac.update(flight=f"{pfx}{rng.randrange(1, 999)}", t=rng.choice(types), r=_reg(regp),
                      hex=new_hex(base), category="A3", ground=True, gs=rng.uniform(0, 24), rate=0,
                      fix=(ap[1] + rng.uniform(-0.012, 0.012), ap[2] + rng.uniform(-0.02, 0.02)))
        ac["brg0"] = rng.uniform(0, 360)
        ac["track"] = rng.uniform(0, 360)
        ac["squawk"] = "".join(str(rng.randrange(0, 8)) for _ in range(4))
        ac["rssi"] = round(-2.5 - (ac.get("dist", 5) / 215) * 28 - rng.uniform(0, 6), 1)
        ac["src"] = rng.choices(["adsb_icao", "mlat", "tisb_icao", "mode_s"], weights=[88, 8, 2, 2])[0]
        ac["msgs"] = rng.randrange(40, 9000)
        fleet.append(ac)

    # The fixed cast (see module docstring).
    for c in fleet[:3]:
        for k in ("ground", "fix", "mil"):
            c.pop(k, None)
    fleet[0].update(flight="EZY47KL", r="G-UZHF", t="A20N", category="A3", squawk="7700", emergency="general",
                    alt=14200, gs=312, rate=-1472, dist=31, brg0=302, track=118, hex="4078a1", src="adsb_icao", rssi=-7.1)
    fleet[1].update(flight="NPAS21", r="G-POLB", t="EC35", category="A7", alt=1150, gs=74, rate=0, dist=2.2,
                    brg0=264, track=95, hex="43c8f9", mil=True, src="adsb_icao", squawk="7041")
    fleet[2].update(flight="BAW117", r="G-XWBC", t="A35K", category="A5", alt=36000, gs=487, rate=0, dist=44,
                    brg0=276, track=283, hex="407c2e", src="adsb_icao", squawk="2157", rssi=-24.3)
    return fleet


FLEET = make_fleet(int(sys.argv[2]) if len(sys.argv) > 2 else 340)


def snapshot() -> dict:
    now = time.time()
    dt = now - T0
    out = []
    for ac in FLEET:
        t = dt + ac["phase"]
        row = {"hex": ac["hex"], "flight": ac["flight"].ljust(8), "r": ac["r"], "t": ac["t"], "category": ac["category"],
               "type": ac["src"], "squawk": ac["squawk"], "rssi": ac["rssi"], "messages": ac["msgs"] + int(dt * 3),
               "seen": round(t % 1.9, 1), "seen_pos": round(t % 2.4, 1)}
        if ac.get("mil"):
            row["dbFlags"] = 1
        if ac.get("emergency"):
            row["emergency"] = ac["emergency"]
        if ac.get("ground"):
            lat, lon = ac["fix"]
            row.update(alt_baro="ground", gs=round(ac["gs"], 1), track=round((ac["track"] + dt) % 360, 1),
                       lat=round(lat + 0.0015 * math.sin(t / 40), 5), lon=round(lon + 0.0025 * math.cos(t / 40), 5))
        else:
            # Each aircraft loops a ~150 nm leg (and a 10-minute climb/descent profile), so the
            # fleet stays in range and at plausible altitudes however long the mock runs.
            lat0, lon0 = dest_point(RX_LAT, RX_LON, ac["brg0"], ac["dist"])
            trk = (ac["track"] + 6 * math.sin(t / 55)) % 360
            lat, lon = dest_point(lat0, lon0, ac["track"], (ac["gs"] * t / 3600) % 150)
            alt = min(43000, max(300, int(ac["alt"] + ac["rate"] * (t % 600) / 60)))
            alt -= alt % 25
            row.update(alt_baro=alt, alt_geom=alt + 275, gs=round(ac["gs"], 1), track=round(trk, 1),
                       baro_rate=ac["rate"], geom_rate=ac["rate"], lat=round(lat, 5), lon=round(lon, 5),
                       nav_qnh=1013.6, nav_altitude_mcp=(36000 if ac["rate"] >= 0 else 6000),
                       ias=round(ac["gs"] * 0.62), tas=round(ac["gs"] * 0.97), mach=round(ac["gs"] / 600, 3),
                       roll=round(4 * math.sin(t / 55), 1), track_rate=round(0.1 * math.cos(t / 55), 2),
                       wd=250, ws=42, oat=-48 if alt > 25000 else 4, true_heading=round(trk, 1))
        out.append(row)
    return {"now": now, "messages": int(1_000_000 + dt * 900), "aircraft": out}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep the console quiet
        pass

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path.endswith("/data/aircraft.json"):
            body = json.dumps(snapshot()).encode()
        elif path.endswith("/data/receiver.json"):
            body = json.dumps({"lat": RX_LAT, "lon": RX_LON, "version": "mock", "refresh": 1000}).encode()
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8771
    print(f"mock tar1090: http://127.0.0.1:{port}/data/aircraft.json ({len(FLEET)} aircraft)", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
