def generate_diva_events_text(chain_counts):
    """
    Generate a formatted string for a DIVA events map given a chain configuration.

    Args:
        chain_counts (list[int]): Number of topics per level, e.g., [1, 2, 1]

    Returns:
        str: Multi-line string ready to copy/paste into event_logic.py
    """
    lines = ["events = {"]
    for level, count in enumerate(chain_counts):
        for idx in range(count):
            # Name the event
            event_name = chr(ord('A') + level)
            if count > 1:
                event_name += str(idx + 1)

            bus_name = f"chain-level-{level}-bus-{idx}"

            # Final level → detector only
            if level == len(chain_counts) - 1:
                lines.append(f"    '{event_name}': {{")
                lines.append(f"        'detect': lambda _: detect_bus_activity('{bus_name}'),")
                lines.append(f"        'role': 'detect',")
                lines.append(f"        'alert': alert,")
                lines.append(f"    }},")
            else:
                # Middle hops → inject + detect
                lines.append(f"    '{event_name}': {{")
                lines.append(f"        'detect': lambda _: detect_bus_activity('{bus_name}'),")
                lines.append(f"        'inject': lambda event_name: inject_to_bus(event_name, '{bus_name}'),")
                lines.append(f"        'alert': alert,")
                lines.append(f"        'reset': {{'on_verify': False}},")
                lines.append(f"    }},")
    lines.append("}")
    return "\n".join(lines)


# -------------------------
# Example usage
# -------------------------
# chain_counts = [1, 2, 1]  # e.g., 1->2->1
# events_text = generate_diva_events_text(chain_counts)
# print(events_text)