"""
device_templates.py — Готовые шаблоны устройств
Гарантируют внутреннюю согласованность фингерпринта:
правильные связки GPU ↔ RAM ↔ CPU ↔ экран.

Случайная генерация каждого поля приводит к нереалистичным
комбинациям (RTX 4090 + 4GB RAM), которые детектор сразу видит.
"""

import random


# ──────────────────────────────────────────────────────────────
# ШАБЛОНЫ УСТРОЙСТВ
# Каждый шаблон — правдоподобный пресет реального устройства
# ──────────────────────────────────────────────────────────────

DEVICE_TEMPLATES = {

    # ─── ИГРОВЫЕ ПК ───────────────────────────────────────────
    "gaming_high_end": {
        "description": "Топовый игровой ПК",
        "webgl_vendor":   "Google Inc. (NVIDIA Corporation)",
        "webgl_renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4090 Direct3D11 vs_5_0 ps_5_0, D3D11)",
        "hardware_concurrency": 24,
        "device_memory": 32,
        "screen_sizes": [(2560, 1440), (3840, 2160), (1920, 1080)],
        "color_depth": 24,
    },
    "gaming_mid": {
        "description": "Средний игровой ПК",
        "webgl_vendor":   "Google Inc. (NVIDIA Corporation)",
        "webgl_renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 Direct3D11 vs_5_0 ps_5_0, D3D11)",
        "hardware_concurrency": 12,
        "device_memory": 16,
        "screen_sizes": [(1920, 1080), (2560, 1440)],
        "color_depth": 24,
    },

    # ─── ОФИСНЫЕ ПК ───────────────────────────────────────────
    "office_desktop": {
        "description": "Офисный стационарный ПК",
        "webgl_vendor":   "Google Inc. (Intel)",
        "webgl_renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)",
        "hardware_concurrency": 8,
        "device_memory": 8,
        "screen_sizes": [(1920, 1080), (1366, 768), (1600, 900)],
        "color_depth": 24,
    },
    "office_laptop": {
        "description": "Офисный ноутбук",
        "webgl_vendor":   "Google Inc. (Intel)",
        "webgl_renderer": "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)",
        "hardware_concurrency": 8,
        "device_memory": 16,
        "screen_sizes": [(1920, 1080), (1536, 864), (1366, 768)],
        "color_depth": 24,
    },

    # ─── БЮДЖЕТНЫЕ МАШИНЫ ─────────────────────────────────────
    "budget_desktop": {
        "description": "Бюджетный ПК",
        "webgl_vendor":   "Google Inc. (AMD)",
        "webgl_renderer": "ANGLE (AMD, AMD Radeon(TM) Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)",
        "hardware_concurrency": 4,
        "device_memory": 4,
        "screen_sizes": [(1366, 768), (1600, 900)],
        "color_depth": 24,
    },

    # ─── ГЕЙМЕРСКИЕ НОУТБУКИ ─────────────────────────────────
    "gaming_laptop": {
        "description": "Игровой ноутбук",
        "webgl_vendor":   "Google Inc. (NVIDIA Corporation)",
        "webgl_renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 Laptop GPU Direct3D11 vs_5_0 ps_5_0, D3D11)",
        "hardware_concurrency": 16,
        "device_memory": 16,
        "screen_sizes": [(1920, 1080), (2560, 1440)],
        "color_depth": 24,
    },

    # ─── AMD-СБОРКИ ───────────────────────────────────────────
    "amd_desktop_mid": {
        "description": "ПК на AMD Ryzen + Radeon",
        "webgl_vendor":   "Google Inc. (AMD)",
        "webgl_renderer": "ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0, D3D11)",
        "hardware_concurrency": 12,
        "device_memory": 16,
        "screen_sizes": [(1920, 1080), (2560, 1440)],
        "color_depth": 24,
    },
}


# ──────────────────────────────────────────────────────────────
# ВЫБОР ШАБЛОНА
# ──────────────────────────────────────────────────────────────

def get_template(name: str = None) -> dict:
    """
    Возвращает копию шаблона + выбранный screen_size.
    name=None — случайный шаблон со взвешенной вероятностью
    (офисные машины встречаются чаще геймерских)
    """
    if name is None:
        # Взвешенный выбор — офисные машины более распространены
        weights = {
            "office_desktop":   25,
            "office_laptop":    25,
            "gaming_mid":       15,
            "gaming_laptop":    10,
            "budget_desktop":   10,
            "amd_desktop_mid":  10,
            "gaming_high_end":  5,
        }
        names   = list(weights.keys())
        weights_list = list(weights.values())
        name = random.choices(names, weights=weights_list, k=1)[0]

    if name not in DEVICE_TEMPLATES:
        raise ValueError(f"Неизвестный шаблон: {name}. Доступные: {list(DEVICE_TEMPLATES.keys())}")

    template = dict(DEVICE_TEMPLATES[name])
    template["template_name"]   = name
    template["screen"]          = random.choice(template.pop("screen_sizes"))
    return template


def list_templates() -> list[str]:
    """Список доступных шаблонов с описаниями"""
    return [
        f"{name:<20} — {cfg['description']}"
        for name, cfg in DEVICE_TEMPLATES.items()
    ]


# ──────────────────────────────────────────────────────────────
# ВАЛИДАТОР — проверка на взаимно-исключающие комбинации
# ──────────────────────────────────────────────────────────────

def validate_fingerprint(fp: dict) -> list[str]:
    """
    Возвращает список предупреждений о неконсистентности.
    Пустой список = фингерпринт выглядит реалистично.
    """
    warnings = []

    cores  = fp.get("hardware_concurrency", 0)
    ram    = fp.get("device_memory", 0)
    render = fp.get("webgl_renderer", "")

    # RTX 40/30 серия обычно идёт с 16+ GB RAM
    if "RTX 40" in render or "RTX 30" in render:
        if ram < 16:
            warnings.append(f"RTX карта + только {ram}GB RAM — подозрительно")
        if cores < 8:
            warnings.append(f"RTX карта + только {cores} ядер — редкая комбинация")

    # Integrated Intel + 32GB RAM — очень редко
    if "Intel(R) UHD Graphics" in render and ram >= 32:
        warnings.append("Integrated Intel Graphics + 32GB RAM — нетипично")

    # Мобильные GPU + 24+ ядер
    if "Laptop" in render and cores > 16:
        warnings.append(f"Laptop GPU + {cores} ядер — проверь консистентность")

    # Экран 4K + слабое железо
    width, height = fp.get("screen_width", 0), fp.get("screen_height", 0)
    if width >= 3840 and ram < 16:
        warnings.append("4K экран + <16GB RAM — нетипично")

    # CPU cores должны быть чётными в большинстве случаев
    if cores not in (1, 2, 4, 6, 8, 10, 12, 14, 16, 20, 24, 32):
        warnings.append(f"Необычное число ядер: {cores}")

    return warnings
