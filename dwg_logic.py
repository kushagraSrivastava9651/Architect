import os
import re
import subprocess
import ezdxf
from shapely.geometry import Polygon, Point
from room_dimension import (check_text_within_room , check_dimensions_match)

ODA_CONVERTER_PATH = "/Applications/ODAFileConverter.app/Contents/MacOS/ODAFileConverter"

def sanitize_filename(filename):
    return re.sub(r'[^\w\-_\. ]', '_', filename)
 
def convert_dwg_to_dxf(dwg_path, output_folder):
    import shutil

    filename_base = os.path.splitext(os.path.basename(dwg_path))[0]
    abs_input_folder = os.path.abspath(os.path.dirname(dwg_path))
    abs_output_folder = os.path.abspath(output_folder)
    os.makedirs(abs_output_folder, exist_ok=True)

    print("Running ODA File Converter...")
    print("Input Folder:", abs_input_folder)
    print("Output Folder:", abs_output_folder)

    try:
        result = subprocess.run([
            ODA_CONVERTER_PATH,
            abs_input_folder,
            abs_output_folder,
            "ACAD2018", "DXF", "0", "1", "*.DWG"
        ], check=True)
    except subprocess.CalledProcessError as e:
        raise Exception(f"ODA Conversion failed: {e}")

    output_dxf_path = os.path.join(abs_output_folder, filename_base + ".dxf")
    if not os.path.exists(output_dxf_path):
        raise Exception(f"[ERROR] Converted DXF file not found: {output_dxf_path}")

    return output_dxf_path
 

def save_copy_with_changes(original, new, rooms, texts):
    doc = ezdxf.readfile(original)

    for room in rooms:
        matched = False
        matched_count, total_count = 0, 0
        block_name = room['BlockName']

        if block_name in texts:
            for t in texts[block_name]:
                if check_text_within_room(room, t):
                    mc, tc = check_dimensions_match(room, [t])
                    matched_count += mc
                    total_count += tc
                    if mc > 0:
                        matched = True

        match_label = f"Match: {matched_count}/{total_count}"

        if block_name in doc.blocks:
            for ent in doc.blocks[block_name]:
                if ent.dxftype() == 'LWPOLYLINE' and ent.dxf.handle == room['Handle']:
                    center = (
                        (room['Polygon'].bounds[0] + room['Polygon'].bounds[2]) / 2,
                        (room['Polygon'].bounds[1] + room['Polygon'].bounds[3]) / 2
                    )
                    doc.modelspace().add_text(match_label, dxfattribs={'height': 5, 'insert': center})
                    ent.dxf.color = 3 if matched else 1

            for t in texts[block_name]:
                if check_text_within_room(room, t):
                    txt_entity = doc.entitydb.get(t['Handle'])
                    if txt_entity:
                        txt_entity.dxf.color = 3 if matched else 1

    doc.saveas(new)
