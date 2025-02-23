import os
import tempfile

import vpype_cli


def svg_to_hpgl_commands(svg_path, pen_speed, pen_number):
    """
    Convert an SVG to HPGL using vpype, then return the HPGL data as a list of processed commands.
    """
    # Create a temporary directory
    with tempfile.TemporaryDirectory() as tmpdir:
        hpgl_path = os.path.join(tmpdir, "output.hpgl")
        
        command_str = (
            f"--config printerconfig/roland_dxy1200.toml "
            f"read '{svg_path}' "
            "layout -h left -v bottom --no-bbox a3 "
            "linesort "
            "linemerge "
            "linesimplify "
            f"write --device roland_dxy1200 --page-size a3 '{hpgl_path}'"
        )
    
        vpype_cli.execute(command_str)
        
        # Read the resulting HPGL file
        with open(hpgl_path, "r", encoding="utf-8", errors="replace") as f:
            hpgl_data = f.read()

    commands = process_hpgl_commands(hpgl_data, pen_number, pen_speed)

    return commands

def split_hpgl_into_commands(hpgl_str):
    """
    Splits a long HPGL string (with multiple commands) into individual commands
    by splitting on semicolons, trimming whitespace, and ignoring empty commands.
    """
    # Split by ';' and strip spaces
    parts = [part.strip() for part in hpgl_str.strip().split(';')]
    # Filter out empty parts that may occur due to trailing semicolons
    return [p for p in parts if p]


def split_command_into_pairs(command_str, max_pairs=50):
    """
    Splits a single PD or PU command with coordinates into multiple commands,
    each with up to max_pairs coordinate pairs.
    """
    # Identify command prefix (e.g., 'PD' or 'PU')
    i = 0
    while i < len(command_str) and command_str[i].isalpha():
        i += 1

    cmd_prefix = command_str[:i]  # 'PD' or 'PU'
    coords_str = command_str[i:].strip()

    if not coords_str:
        # No coordinates, just return as a single command with semicolon
        return [cmd_prefix + ';']

    # Split into individual numbers
    numbers = coords_str.split(',')
    if len(numbers) % 2 != 0:
        raise ValueError(f"Odd number of coordinate values in {command_str}, pairs are incomplete.")

    pairs = list(zip(numbers[0::2], numbers[1::2]))  # [(x1,y1), (x2,y2), ...]
    
    chunked_commands = []
    for start in range(0, len(pairs), max_pairs):
        chunk = pairs[start:start+max_pairs]
        coords_chunk = ",".join(f"{x},{y}" for x,y in chunk)
        chunked_commands.append(f"{cmd_prefix}{coords_chunk};")

    return chunked_commands


def process_hpgl_commands(hpgl_str, pen_number, pen_speed, max_pairs=50):
    """
    Processes an HPGL string that may contain multiple commands. Splits large
    PD or PU commands containing coordinates into chunks of up to max_pairs.
    Overrides pen number and speed commands.
    Returns a list of commands.
    """
    commands = split_hpgl_into_commands(hpgl_str)
    result = []

    for cmd in commands:
        if cmd.startswith('PD') or cmd.startswith('PU'):
            # Split PD/PU commands with coordinates into chunks
            split_cmds = split_command_into_pairs(cmd, max_pairs=max_pairs)
            result.extend(split_cmds)
        elif cmd.startswith('SP'):
            # Override pen number and set speed
              result.append(f"SP{pen_number};")
              result.append(f"VS{pen_speed};")
        else:
            # Everything else, just append
            result.append(cmd + ';' if not cmd.endswith(';') else cmd)

    return result
