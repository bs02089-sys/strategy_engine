from typing import Union

def format_percent(value: Union[float, int, str], default: str = "N/A") -> str:
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return default