import os
import tempfile

import vpype_cli

from color_utils import extract_colors_from_svg, split_svg_by_color


def svg_to_hpgl_commands(svg_path, pen_speed, pen_number):
    """
    Convert an SVG to HPGL using vpype, then return the HPGL data as a list of processed commands.
    Uses a single pen number for all content (legacy mode).
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


def svg_to_hpgl_commands_multicolor(svg_path, color_to_pen: dict[str, int], pen_speed: int = 1):
    """
    Convert a multi-color SVG to HPGL with automatic pen changes.

    Args:
        svg_path: Path to the SVG file
        color_to_pen: Dict mapping hex color (e.g., '#FF0000') to pen number (1-8)
        pen_speed: Speed setting for all pens

    Returns:
        List of HPGL commands with SP (select pen) commands between color groups
    """
    # Split SVG by color
    color_svgs = split_svg_by_color(svg_path)

    if not color_svgs:
        # Fallback to single color processing
        return svg_to_hpgl_commands(svg_path, pen_speed, 1)

    all_commands = []

    # Process each color group
    for color, svg_content in color_svgs.items():
        pen_number = color_to_pen.get(color.upper(), 1)

        # Write temporary SVG file for this color
        with tempfile.TemporaryDirectory() as tmpdir:
            temp_svg_path = os.path.join(tmpdir, "color_layer.svg")
            hpgl_path = os.path.join(tmpdir, "output.hpgl")

            with open(temp_svg_path, "w", encoding="utf-8") as f:
                f.write(svg_content)

            # Convert this color layer to HPGL
            command_str = (
                f"--config printerconfig/roland_dxy1200.toml "
                f"read '{temp_svg_path}' "
                "layout -h left -v bottom --no-bbox a3 "
                "linesort "
                "linemerge "
                "linesimplify "
                f"write --device roland_dxy1200 --page-size a3 '{hpgl_path}'"
            )

            vpype_cli.execute(command_str)

            # Read HPGL output
            with open(hpgl_path, "r", encoding="utf-8", errors="replace") as f:
                hpgl_data = f.read()

        # Process commands for this color (without adding SP/VS since we'll add them)
        color_commands = process_hpgl_commands_raw(hpgl_data)

        if color_commands:
            # Add pen selection and speed before this color's commands
            all_commands.append(f"SP{pen_number};")
            all_commands.append(f"VS{pen_speed};")
            all_commands.extend(color_commands)

    return all_commands


def process_hpgl_commands_raw(hpgl_str, max_pairs=50):
    """
    Process HPGL commands without adding pen selection/speed.
    Used for multi-color processing where pen selection is handled separately.
    """
    commands = split_hpgl_into_commands(hpgl_str)
    result = []

    for cmd in commands:
        if cmd.startswith('PD') or cmd.startswith('PU'):
            # Split PD/PU commands with coordinates into chunks
            split_cmds = split_command_into_pairs(cmd, max_pairs=max_pairs)
            result.extend(split_cmds)
        elif cmd.startswith('SP') or cmd.startswith('VS'):
            # Skip pen selection and speed - handled by multicolor wrapper
            continue
        elif cmd.startswith('IN'):
            # Skip initialize commands between color groups
            continue
        else:
            # Everything else, just append
            result.append(cmd + ';' if not cmd.endswith(';') else cmd)

    return result

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
