"""
Color extraction utilities for SVG files.
Uses svgelements to parse SVG and extract stroke colors for pen assignment.
"""
from svgelements import SVG, Path, Shape, Color


def extract_colors_from_svg(svg_path: str) -> list[str]:
    """
    Extract unique stroke colors from an SVG file.

    Args:
        svg_path: Path to the SVG file

    Returns:
        List of unique hex color strings (e.g., ['#000000', '#ff0000'])
    """
    svg = SVG.parse(svg_path)
    colors = set()

    for element in svg.elements():
        # Check for stroke color on paths and shapes
        if hasattr(element, 'stroke') and element.stroke is not None:
            stroke = element.stroke
            # Handle Color objects
            if isinstance(stroke, Color):
                if stroke.value is not None:
                    hex_color = stroke.hex
                    if hex_color:
                        colors.add(hex_color.upper())
            elif isinstance(stroke, str) and stroke.startswith('#'):
                colors.add(stroke.upper())

    # Sort for consistent ordering (black first, then by hex value)
    sorted_colors = sorted(colors, key=lambda c: (c != '#000000', c))
    return sorted_colors


def get_elements_by_color(svg_path: str) -> dict[str, list]:
    """
    Group SVG elements by their stroke color.

    Args:
        svg_path: Path to the SVG file

    Returns:
        Dict mapping hex color to list of elements with that stroke color
    """
    svg = SVG.parse(svg_path)
    color_groups = {}

    for element in svg.elements():
        if hasattr(element, 'stroke') and element.stroke is not None:
            stroke = element.stroke
            hex_color = None

            if isinstance(stroke, Color):
                if stroke.value is not None:
                    hex_color = stroke.hex
                    if hex_color:
                        hex_color = hex_color.upper()
            elif isinstance(stroke, str) and stroke.startswith('#'):
                hex_color = stroke.upper()

            if hex_color:
                if hex_color not in color_groups:
                    color_groups[hex_color] = []
                color_groups[hex_color].append(element)

    return color_groups


def create_single_color_svg(svg_path: str, target_color: str) -> str:
    """
    Create SVG content containing only elements of a specific color.
    All elements are converted to black stroke for vpype processing.

    Args:
        svg_path: Path to the original SVG file
        target_color: Hex color string to filter (e.g., '#FF0000')

    Returns:
        SVG content as a string with only matching elements (strokes set to black)
    """
    svg = SVG.parse(svg_path)
    target_upper = target_color.upper()

    # Build list of elements to keep
    elements_to_keep = []

    for element in svg.elements():
        if hasattr(element, 'stroke') and element.stroke is not None:
            stroke = element.stroke
            hex_color = None

            if isinstance(stroke, Color):
                if stroke.value is not None:
                    hex_color = stroke.hex
                    if hex_color:
                        hex_color = hex_color.upper()
            elif isinstance(stroke, str) and stroke.startswith('#'):
                hex_color = stroke.upper()

            if hex_color == target_upper:
                # Set stroke to black for vpype processing
                element.stroke = Color('#000000')
                elements_to_keep.append(element)

    # Build new SVG with only the filtered elements
    if not elements_to_keep:
        return None

    # Get viewBox from original
    viewbox = svg.viewbox
    width = svg.width if svg.width else "100%"
    height = svg.height if svg.height else "100%"

    # Build SVG string
    svg_parts = [f'<svg xmlns="http://www.w3.org/2000/svg"']

    if viewbox:
        svg_parts.append(f' viewBox="{viewbox.x} {viewbox.y} {viewbox.width} {viewbox.height}"')
    if width:
        svg_parts.append(f' width="{width}"')
    if height:
        svg_parts.append(f' height="{height}"')
    svg_parts.append('>')

    # Add path elements
    for element in elements_to_keep:
        if isinstance(element, Path):
            d = element.d()
            svg_parts.append(f'<path d="{d}" stroke="#000000" fill="none"/>')
        elif hasattr(element, 'd'):
            d = element.d()
            svg_parts.append(f'<path d="{d}" stroke="#000000" fill="none"/>')

    svg_parts.append('</svg>')

    return ''.join(svg_parts)


def split_svg_by_color(svg_path: str) -> dict[str, str]:
    """
    Split an SVG file into separate SVG content per color.

    Args:
        svg_path: Path to the SVG file

    Returns:
        Dict mapping hex color to SVG content string for that color
    """
    colors = extract_colors_from_svg(svg_path)
    result = {}

    for color in colors:
        svg_content = create_single_color_svg(svg_path, color)
        if svg_content:
            result[color] = svg_content

    return result
