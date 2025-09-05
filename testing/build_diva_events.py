import argparse

def generate_diva_events_text(chain_counts: list[int]) -> str:
    """
    Generate DIVA events map text for a chain of Lambda functions.
    Each event corresponds to a source→target hop.

    Args:
        chain_counts (list[int]): Number of functions per level
        e.g., [1, 2, 1] means A→(B1,B2)→C.

    Returns:
        str: Multi-line string ready to copy/paste into event_logic.py
    """
    lines = ["events = {"]
    for level in range(len(chain_counts)):
        # last_level = level == len(chain_counts) - 1
        first_level = level == 0
        for src_idx in range(chain_counts[level]):
            lambda_name = f"node-{level}-{src_idx}"

            lines.append(f"    '{lambda_name}': {{")
            lines.append(f"        'detect': detect_lambda_activity,")
            if first_level:
                lines.append(f"        'inject': inject_to_lambda,")
            else:
                lines.append(f"        'role': 'detect',")
            lines.append(f"        'alert': alert,")
            lines.append(f"        'warmup': {{'enabled': True, 'success_threshold': 2}},")
            lines.append(f"        'reset': {{'mode': 'cooldown', 'success_threshold': 2}},")
            lines.append(f"    }},")
    lines.append("}")
    return "\n".join(lines)




# -------------------------
# Example usage
# -------------------------
# python build_diva_events.py 1,2,1
# -------------------------


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generete DIVA events map event_logic.py")
    parser.add_argument("chain", help="Comma-separated list of ints (e.g. 2,3,1,1)")
    args = parser.parse_args()

    chain_spec = [int(x) for x in args.chain.split(",")]
    events_text = generate_diva_events_text(chain_spec)
    print(events_text)