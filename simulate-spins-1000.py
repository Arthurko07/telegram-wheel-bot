import json
import os
import random
from collections import Counter

PRIZES_FILE = "prizes.json"
SPINS = 1000


def read_json_file(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def normalize_weight(value, default=1.0):
    try:
        weight = float(value)
    except (TypeError, ValueError):
        return float(default)
    if weight <= 0:
        return float(default)
    return round(weight, 3)


def choose_weighted_prize(prizes):
    active_prizes = [p for p in prizes if p.get("active", True)]
    if not active_prizes:
        return None

    total_weight = sum(normalize_weight(p.get("weight", 1)) for p in active_prizes)
    if total_weight <= 0:
        return None

    threshold = random.uniform(0, total_weight)
    cumulative = 0.0

    for prize in active_prizes:
        cumulative += normalize_weight(prize.get("weight", 1))
        if threshold <= cumulative:
            return prize

    return active_prizes[-1]


def main():
    prizes = read_json_file(PRIZES_FILE, [])
    active_prizes = [p for p in prizes if p.get("active", True)]

    if not active_prizes:
        print("Нет активных призов в prizes.json")
        return

    total_weight = sum(normalize_weight(p.get("weight", 1)) for p in active_prizes)
    counter = Counter()

    for _ in range(SPINS):
        prize = choose_weighted_prize(active_prizes)
        if prize:
            counter[int(prize.get("id", 0))] += 1

    print(f"Симуляция {SPINS} спинов\n")
    print("Ожидаемый и фактический результат:\n")

    for prize in active_prizes:
        prize_id = int(prize.get("id", 0))
        title = prize.get("title", "Без названия")
        weight = normalize_weight(prize.get("weight", 1))
        expected_percent = (weight / total_weight) * 100 if total_weight > 0 else 0
        actual_count = counter.get(prize_id, 0)
        actual_percent = (actual_count / SPINS) * 100 if SPINS > 0 else 0

        print(f"ID: {prize_id}")
        print(f"Приз: {title}")
        print(f"Вес: {weight}")
        print(f"Ожидаемый шанс: {expected_percent:.2f}%")
        print(f"Выпало: {actual_count} раз")
        print(f"Фактический шанс: {actual_percent:.2f}%")
        print("-" * 40)


if __name__ == "__main__":
    main()
