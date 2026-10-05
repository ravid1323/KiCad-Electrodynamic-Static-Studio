import os
import sys
import re
import json
import numpy as np

try:
    from matplotlib import pyplot as plt
    HAS_MATPLOTLIB = True
except ImportError:
    HAS_MATPLOTLIB = False
    print("[!] Warning: 'matplotlib' library is missing! Run: pip install matplotlib")

try:
    from shapely.geometry import Polygon, LineString, Point
    from shapely.ops import triangulate, unary_union
    from shapely.geometry import box
    from shapely.affinity import scale

    HAS_SHAPELY = True
except ImportError:
    HAS_SHAPELY = False
    print("[!] Warning: 'shapely' library is missing! Run: pip install shapely")

dll_path = r'C:\openEMS'
if os.path.exists(dll_path):
    os.add_dll_directory(dll_path)
    os.environ['PATH'] = dll_path + os.pathsep + os.environ.get('PATH', '')

from CSXCAD import AppCSXCAD_BIN
from CSXCAD.CSXCAD import ContinuousStructure
import openEMS
from openEMS import ports


GRID_Q = 1e-3  # 1 um quantization
def q(v):
    return round(float(v) / GRID_Q) * GRID_Q

def to_csx(x, y, center_x, center_y, snap=0.0):
    """
    Convert coordinates to CSXCAD relative to center, strictly quantized.
    """
    rel_x = q(float(x - center_x))
    rel_y = q(float(y - center_y))

    if snap and snap > 0:
        rel_x = q(round(rel_x / snap) * snap)
        rel_y = q(round(rel_y / snap) * snap)

    return rel_x, rel_y


class SimulationConfig:
    def __init__(self, json_path):
        fallback_dir = os.path.dirname(os.path.abspath(json_path))
        if not os.path.exists(json_path):
            raise FileNotFoundError(f"JSON file not found: {json_path}")
        with open(json_path, 'r', encoding='utf-8') as f:
            self.raw_data = json.load(f)
        self.sim_dir = self.raw_data.get("simulation_dir", fallback_dir)
        if os.name == 'posix' and '\\' in self.sim_dir:
            self.sim_dir = self.sim_dir.replace('\\', '/')
            if len(self.sim_dir) > 1 and self.sim_dir[1] == ':':
                self.sim_dir = '/mnt/' + self.sim_dir[0].lower() + self.sim_dir[2:]
        self.sim_type = str(self.raw_data.get("simulation_type", "2.5D")).upper()
        self.is_2_5d = (self.sim_type == "2.5D")
        self.boundary_conditions = self.raw_data.get("boundary_conditions", {
            "x_neg": "PML", "x_pos": "PML",
            "y_neg": "PML", "y_pos": "PML",
            "z_neg": "PML", "z_pos": "PML"
        })
        self.TRANSPARENT_DIELECTRICS = False 
        margins = self.raw_data.get("air_margins_mm", {})
        self.margin_x_neg = float(margins.get("x_neg", 5.0))
        self.margin_x_pos = float(margins.get("x_pos", 5.0))
        self.margin_y_neg = float(margins.get("y_neg", 5.0))
        self.margin_y_pos = float(margins.get("y_pos", 5.0))
        self.margin_z_neg = float(margins.get("z_neg", 4.0))
        self.margin_z_pos = float(margins.get("z_pos", 4.0))
        self.use_pec_copper = bool(self.raw_data.get("use_pec_copper", True))
        self.mesh_mode = self.raw_data.get("mesh_mode", "Advanced")
        self.mesh_global = self.raw_data.get("mesh_global", {
            "x": {"max_size": 1.0, "growth": "Exponential", "ratio": 1.3},
            "y": {"max_size": 1.0, "growth": "Exponential", "ratio": 1.3},
            "z": {"max_size": 0.5, "growth": "Exponential", "ratio": 1.25}
        })
        self.mesh_feature = self.raw_data.get("mesh_feature", {"x_cells": 4, "y_cells": 4, "z_cells": 3})
        self.mesh_gap = self.raw_data.get("mesh_gap", {"x_cells": 3, "y_cells": 3, "z_cells": 3})
        self.mesh_locks = self.raw_data.get("mesh_locks", {
            "conductor": True, "via": True, "pad": True, "dielectric": True, "port": True
        })

        self.crop_margin_mm = float(self.raw_data.get("crop_margin_mm", 5.0))
        self.min_cell_size_mm = float(self.raw_data.get("min_cell_size_mm", 0.04))
        self.crop_to_active_traces = bool(self.raw_data.get("crop_to_active_traces", True))
        self.default_antipad_clearance_mm = float(self.raw_data.get("default_antipad_clearance_mm", 0.25))
        self.ambient_epsilon = float(self.raw_data.get("ambient_epsilon", 1.0))
        self.ambient_mue = float(self.raw_data.get("ambient_mue", 1.0))

        self.f_start = float(self.raw_data.get("f_start_ghz", 0.0)) * 1e9
        self.f_stop = float(self.raw_data.get("f_stop_ghz", 10.0)) * 1e9
        self.f_max = float(self.raw_data.get("f_max_ghz", 12.0)) * 1e9
        self.f_num_points = int(self.raw_data.get("f_num_points", 1001))
        self.port_reference_impedance = float(
            self.raw_data.get("port_reference_impedance", 50.0)
        )
        # --- Advanced 4-Port Sweep Settings ---
        self.num_threads = int(self.raw_data.get("threads", 0))
        self.assume_symmetry = bool(self.raw_data.get("assume_symmetry", False))
        self.parallel_sim = bool(self.raw_data.get("parallel_sim", False))

        # Safely determine the extraction goal based on the actual Port Mode dropdowns!
        port_modes = [p.get("mode", "Single-Ended") for p in self.raw_data.get("manual_ports", {}).values()]

        # Only trigger the heavy multi-run matrix sweep if the ports are actually in Single-Ended mode
        self.total_physical_ports = 0
        for p in self.raw_data.get("manual_ports", {}).values():
            self.total_physical_ports += 1
            if p.get("mode") != "Single-Ended" and "negative_terminal" in p:
                self.total_physical_ports += 1

        # Trigger rigorous full-matrix sweep if >2 physical ports are configured
        self.extract_matrix = self.total_physical_ports > 2
        self.f_center = (self.f_start + self.f_max) / 2.0
        self.f_bandwidth = self.f_max - self.f_start
        self.excitation_type = self.raw_data.get("excitation_type", "Gaussian")
        self.energy_limit_db = float(self.raw_data.get("energy_limit_db", -30.0))
        self.max_timesteps = int(self.raw_data.get("max_timesteps", 100000))

        pml_setting = str(self.raw_data.get("pml_cells", "PML_8"))
        pml_match = re.search(r'\d+', pml_setting)
        self.pml_cells_count = int(pml_match.group()) if pml_match else 8

        self.all_tracks, self.all_vias, self.all_tht_pads, self.all_smd_pads, self.all_zones = [], [], [], [], []
        for net_data in self.raw_data.get('target_nets', []):
            net_name = net_data.get('name', 'Unknown')
            # Fetch the dynamic clearance, default to safe 0.25mm
            net_clearance = float(net_data.get('clearance_mm', 0.25))

            # Inject the net_name and clearance into EVERY item
            for track in net_data.get('tracks', []):
                track['net_name'] = net_name
                track['clearance_mm'] = net_clearance
                self.all_tracks.append(track)

            for via in net_data.get('vias', []):
                via['net_name'] = net_name
                via['clearance_mm'] = net_clearance
                self.all_vias.append(via)

            for pad in net_data.get('tht_pads', []):
                pad['net_name'] = net_name
                pad['clearance_mm'] = net_clearance
                self.all_tht_pads.append(pad)

            for pad in net_data.get('smd_pads', []):
                pad['net_name'] = net_name
                pad['clearance_mm'] = net_clearance
                self.all_smd_pads.append(pad)

            for zone in net_data.get('zones', []):
                zone['net_name'] = net_name
                zone['clearance_mm'] = net_clearance
                self.all_zones.append(zone)

        self.all_pads = self.all_tht_pads + self.all_smd_pads
        self._calculate_unified_bounds()
        self.layer_z = self._calculate_layer_z()

    def _calculate_unified_bounds(self):
        edge_cuts = self.raw_data.get('edge_cuts', [])
        xs, ys = [], []

        # Primary Boundary: Use exact physical board limits from Edge.Cuts
        if edge_cuts:
            for edge in edge_cuts:
                xs.extend([edge['start_x'], edge['end_x']])
                ys.extend([edge['start_y'], edge['end_y']])

        # Fallback Boundary: If no Edge.Cuts exist, bound by active copper
        else:
            for track in self.all_tracks:
                w = track.get('width_mm', 0.2) / 2.0
                xs.extend([track['start_x'] - w, track['start_x'] + w, track['end_x'] - w, track['end_x'] + w])
            for feat in self.all_vias + self.all_pads:
                r = feat.get('size_mm', 1.0) / 2.0
                xs.extend([feat['x'] - r, feat['x'] + r])
                ys.extend([feat['y'] - r, feat['y'] + r])
            for zone in self.all_zones:
                for p in zone.get('points', []):
                    xs.append(p['x'])
                    ys.append(p['y'])

        if xs and ys:
            self.min_x_raw, self.max_x_raw = min(xs), max(xs)
            self.min_y_raw, self.max_y_raw = min(ys), max(ys)
        else:
            self.min_x_raw, self.max_x_raw = -20.0, 20.0
            self.min_y_raw, self.max_y_raw = -20.0, 20.0

        self.center_x = (self.min_x_raw + self.max_x_raw) / 2.0
        self.center_y = (self.min_y_raw + self.max_y_raw) / 2.0

        # The physical FR4 substrate should match the exact PCB footprint.
        # Do NOT add 'crop_margin_mm' here. The air padding is applied purely to the mesh lines later.
        self.board_min_x = self.min_x_raw - self.center_x
        self.board_max_x = self.max_x_raw - self.center_x
        self.board_min_y = self.min_y_raw - self.center_y
        self.board_max_y = self.max_y_raw - self.center_y

    def _calculate_layer_z(self):
        layer_z = {}
        current_z = 0.0
        for layer in self.raw_data.get('stackup', []):
            l_name = layer.get('layer_name', '')
            l_thick = float(layer.get('thickness_mm', 0.0))
            l_type = layer.get('type', 'Dielectric')

            if l_type == 'Copper':
                layer_z[l_name] = {"z": current_z, "type": "Copper", "thickness": l_thick}
                # ONLY subtract copper thickness if we are in true 3D mode
                if not self.is_2_5d:
                    current_z -= l_thick
            else:
                layer_z[l_name] = {
                    "start": current_z,
                    "end": current_z - l_thick,
                    "type": "Dielectric",
                    "thickness": l_thick,
                    "epsilon_r": float(layer.get("epsilon_r", 1.0))
                }
                current_z -= l_thick
        return layer_z


class GeometryBuilder:
    def __init__(self, CSX, config: SimulationConfig, mesh_mgr=None):
        self.CSX = CSX
        self.config = config
        self.layer_metals = {}
        self.mesh_mgr = mesh_mgr

        # Material base colors
        self.BASE_COPPER = '#D4AF37'
        self.BASE_FR4 = '#228B22' 
        self.BASE_MASK = '#006400'

        self.copper_count = 0
        self.fr4_count = 0
        self.mask_count = 0

        self.key_x_points = [self.config.board_min_x, self.config.board_max_x]
        self.key_y_points = [self.config.board_min_y, self.config.board_max_y]
        self.key_z_points = []

    def _get_shaded_color(self, hex_color, index, step=20):
        """פונקציה המחשבת הצללה: מכהה את צבע הבסיס ככל שמעמיקים בשכבות"""
        hex_color = hex_color.lstrip('#')
        r, g, b = tuple(int(hex_color[i:i + 2], 16) for i in (0, 2, 4))

        r = max(0, r - index * step)
        g = max(0, g - index * step)
        b = max(0, b - index * step)
        return f"#{r:02X}{g:02X}{b:02X}"

    def _snap_to_boundary(self, val, axis):
        """Forces trace ends to mathematically hit the PML if trace cropping is enabled."""
        if not getattr(self.config, 'crop_to_active_traces', True):
            return val

        tol = 0.05  # 50 microns

        if axis == 'x':
            if self.config.margin_x_neg == 0.0 and abs(val - self.config.board_min_x) <= tol: return self.config.board_min_x
            if self.config.margin_x_pos == 0.0 and abs(val - self.config.board_max_x) <= tol: return self.config.board_max_x
        elif axis == 'y':
            if self.config.margin_y_neg == 0.0 and abs(val - self.config.board_min_y) <= tol: return self.config.board_min_y
            if self.config.margin_y_pos == 0.0 and abs(val - self.config.board_max_y) <= tol: return self.config.board_max_y

        return val

    def build_materials_and_dielectrics(self):
        alpha_val = 120 if self.config.TRANSPARENT_DIELECTRICS else 255

        # Initialize Air Box Material ONLY (Do not draw it yet, do not add to key_points)
        if not hasattr(self, 'air_box_mat'):
            self.air_box_mat = self.CSX.AddMaterial('Air_Background', epsilon=self.config.ambient_epsilon, mue=self.config.ambient_mue, kappa=0.0)
            self.air_box_mat.SetColor('#E0FFFF', 50)

        for l_name, l_data in self.config.layer_z.items():
            safe_name = l_name.replace('.', '_')

            if l_data['type'] == 'Copper':
                self.key_z_points.append(l_data['z'])
                cond = float(l_data.get('conductivity', 58e6))

                if self.config.use_pec_copper:
                    if not hasattr(self, 'pec_mat'):
                        self.pec_mat = self.CSX.AddMetal('PEC')
                    copper_prop = self.pec_mat
                else:
                    if self.config.is_2_5d:
                        # 2.5D Lossy: Infinitely thin sheet with finite conductivity
                        copper_prop = self.CSX.AddConductingSheet(
                            f'Copper_{safe_name}',
                            conductivity=cond,
                            thickness=l_data['thickness'] * 1e-3
                        )
                    else:
                        # 3D Lossy: Volumetric material with finite conductivity
                        copper_prop = self.CSX.AddMaterial(
                            f'Copper_{safe_name}',
                            epsilon=1.0,
                            mue=1.0,
                            kappa=cond
                        )

                shaded_color = self._get_shaded_color(self.BASE_COPPER, self.copper_count, step=20)
                copper_prop.SetColor(shaded_color)
                self.layer_metals[l_name] = copper_prop
                self.copper_count += 1

            elif l_data['type'] == 'Dielectric' and l_data['thickness'] > 0:
                if l_data['epsilon_r'] > 1.0:
                    self.key_z_points.extend([l_data['start'], l_data['end']])

                kappa = 0.0
                loss_tan = float(l_data.get('loss_tangent', 0.0))
                if loss_tan > 0:
                    kappa = 2.0 * np.pi * self.config.f_center * 8.8541878128e-12 * l_data['epsilon_r'] * loss_tan

                sub = self.CSX.AddMaterial(f'Sub_{safe_name}', epsilon=max(1.0, l_data['epsilon_r']), kappa=kappa)
                sub.AddBox(
                    [self.config.board_min_x, self.config.board_min_y, l_data['start']],
                    [self.config.board_max_x, self.config.board_max_y, l_data['end']],
                    priority=9700
                )

                if 'Mask' in l_name:
                    shaded_color = self._get_shaded_color(self.BASE_MASK, self.mask_count, step=15)
                    self.mask_count += 1
                else:
                    shaded_color = self._get_shaded_color(self.BASE_FR4, self.fr4_count, step=20)
                    self.fr4_count += 1

                sub.SetColor(shaded_color, alpha_val)

    def draw_copper_features(self):
        layer_shapes = {l_name: [] for l_name in self.layer_metals.keys()}

        # Collect Tracks ---
        for track in self.config.all_tracks:
            l_name = track.get('layer', 'F.Cu')
            if l_name not in layer_shapes: continue

            w = track.get('width_mm', 0.2)
            x1, y1 = to_csx(track['start_x'], track['start_y'], self.config.center_x, self.config.center_y)
            x2, y2 = to_csx(track['end_x'], track['end_y'], self.config.center_x, self.config.center_y)

            x1, x2 = self._snap_to_boundary(x1, 'x'), self._snap_to_boundary(x2, 'x')
            y1, y2 = self._snap_to_boundary(y1, 'y'), self._snap_to_boundary(y2, 'y')

            self.key_x_points.extend([x1 - w / 2, x1 + w / 2, x2 - w / 2, x2 + w / 2])
            self.key_y_points.extend([y1 - w / 2, y1 + w / 2, y2 - w / 2, y2 + w / 2])

            # --- THE TRACE MESH ---
            track_length = np.hypot(x2 - x1, y2 - y1)
            is_diagonal = abs(x2 - x1) > 1e-3 and abs(y2 - y1) > 1e-3

            # 1. Master Toggle (Add a checkbox in your GUI: "Force Mesh on Diagonal Traces")
            mesh_diagonals = self.config.raw_data.get("mesh_diagonal_traces", True)

            # 2. Density Control (Add a spinbox in your GUI: "Diagonal Cells per Trace Width")
            # Defaulting to 1 guarantees physical connectivity without microscopic overmeshing.
            trace_cells = int(self.config.mesh_feature.get("trace_cells", 1))

            # ONLY inject if enabled and trace is diagonal
            if is_diagonal and track_length > w and mesh_diagonals and trace_cells > 0:
                step_size = w / trace_cells
                num_steps = int(track_length / step_size)

                if num_steps > 1:
                    for i in range(1, num_steps):
                        frac = i / float(num_steps)
                        mx = x1 + frac * (x2 - x1)
                        my = y1 + frac * (y2 - y1)
                        self.key_x_points.extend([mx - w / 2, mx + w / 2])
                        self.key_y_points.extend([my - w / 2, my + w / 2])

            if np.hypot(x2 - x1, y2 - y1) < 1e-3: continue

            line = LineString([(x1, y1), (x2, y2)])
            layer_shapes[l_name].append(line.buffer(w / 2.0, resolution=4))

        # Collect ALL Pads (SMD & THT 2D Copper) ---
        for pad in self.config.all_smd_pads + self.config.all_tht_pads:
            px_base, py_base = to_csx(pad['x'], pad['y'], self.config.center_x, self.config.center_y)

            for p_shape in pad.get('pad_shapes', []):
                l_name = p_shape.get('layer', 'F.Cu')
                if l_name not in layer_shapes: continue

                px = px_base + p_shape.get('offset_x', 0.0)
                py = py_base + p_shape.get('offset_y', 0.0)
                sx = p_shape.get('size_x_mm', 1.0) / 2.0
                sy = p_shape.get('size_y_mm', 1.0) / 2.0

                # Add pad bounding edges
                self.key_x_points.extend([px - sx, px + sx])
                self.key_y_points.extend([py - sy, py + sy])

                if self.mesh_mgr:
                    self.mesh_mgr.lock('x', px - sx, px + sx)
                    self.mesh_mgr.lock('y', py - sy, py + sy)

                # Enforce user settings across the pad feature
                x_cells = self.config.mesh_feature.get("x_cells", 3)
                y_cells = self.config.mesh_feature.get("y_cells", 3)

                # Inject uniform lines directly across the pad
                if self.mesh_mgr:
                    self.mesh_mgr.add_override_region('x', px - sx, px + sx, x_cells, mode='uniform')
                    self.mesh_mgr.add_override_region('y', py - sy, py + sy, y_cells, mode='uniform')
                shape_type = p_shape.get('shape', 'PSS_RECT')

                if 'CIRCLE' in shape_type or 'ROUND' in shape_type or 'OVAL' in shape_type:
                    if abs(sx - sy) < 1e-4:
                        layer_shapes[l_name].append(Point(px, py).buffer(sx, resolution=12))
                    else:
                        circle = Point(px, py).buffer(1.0, resolution=12)
                        layer_shapes[l_name].append(scale(circle, xfact=sx, yfact=sy))
                else:
                    layer_shapes[l_name].append(box(px - sx, py - sy, px + sx, py + sy))

        # Collect Zones (With Native KiCad Holes) ---
        for zone in self.config.all_zones:
            l_name = zone.get('layer', 'F.Cu')
            if l_name not in layer_shapes: continue

            pts_x, pts_y = [], []
            for p in zone.get('points', []):
                px, py = to_csx(p['x'], p['y'], self.config.center_x, self.config.center_y)
                px = self._snap_to_boundary(px, 'x')
                py = self._snap_to_boundary(py, 'y')
                pts_x.append(px)
                pts_y.append(py)

            if len(pts_x) < 3: continue

            self.key_x_points.extend([min(pts_x), max(pts_x)])
            self.key_y_points.extend([min(pts_y), max(pts_y)])

            exterior = list(zip(pts_x, pts_y))

            interiors = []
            for hole in zone.get('holes', []):
                hx, hy = [], []
                for p in hole:
                    px, py = to_csx(p['x'], p['y'], self.config.center_x, self.config.center_y)
                    hx.append(px)
                    hy.append(py)
                if len(hx) >= 3:
                    interiors.append(list(zip(hx, hy)))

            poly = Polygon(exterior, interiors).buffer(0)
            layer_shapes[l_name].append(poly)

        # EXECUTE 2D UNION AND DRAW LAYER COPPER ---
        dielectric_epsilon_r = self.config.layer_z.get('dielectric 1', {}).get('epsilon_r', 4.2)
        if not hasattr(self, 'void_mat'):
            self.void_mat = self.CSX.AddMaterial('Substrate_Void', epsilon=dielectric_epsilon_r, kappa=0.0)
            self.void_mat.SetColor(self.BASE_FR4, 255)

        for l_name, shapes in layer_shapes.items():
            if not shapes: continue

            metal = self.layer_metals[l_name]
            z_val = self.config.layer_z[l_name]['z']
            c_thick = self.config.layer_z[l_name].get('thickness', 0.0) if not self.config.is_2_5d else 0.0
            z_elev = z_val - c_thick if c_thick > 0 else z_val

            if c_thick > 0:
                # Only force internal skin-effect grid lines if the copper is extremely thick
                if c_thick > self.config.min_cell_size_mm * 2:
                    self.key_z_points.extend(np.linspace(z_elev, z_val, 3))
                else:
                    self.key_z_points.extend([z_elev, z_val])

            # Boolean Union! This mathematically melts all tracks, pads, and zones into one shape
            merged_poly = unary_union(shapes).simplify(0.002, preserve_topology=True)
            polygons = [merged_poly] if merged_poly.geom_type == 'Polygon' else list(merged_poly.geoms)

            for p in polygons:
                if p.is_empty: continue

                # Draw solid exterior (Priority 9800)
                t_coords = list(p.exterior.coords)
                metal.AddLinPoly([[c[0] for c in t_coords], [c[1] for c in t_coords]], 'z', z_elev, c_thick,
                                 priority=9800)

                # Punch native holes/antipads using Priority 9850 Voids
                for interior in p.interiors:
                    i_coords = list(interior.coords)
                    self.void_mat.AddLinPoly([[c[0] for c in i_coords], [c[1] for c in i_coords]], 'z', z_elev - 0.01,
                                             c_thick + 0.02, priority=9850)

        # Draw 3D Via Barrels ---
        z_top = max([l['z'] for l in self.config.layer_z.values() if l.get('type') == 'Copper'], default=0.0)
        z_bottom = min(
            [l['z'] - l.get('thickness', 0.0) for l in self.config.layer_z.values() if l.get('type') == 'Copper'],
            default=-1.5)

        fallback_metal = self.CSX.AddMetal('Copper_Vias_Pads_Default')
        fallback_metal.SetColor(self.BASE_COPPER)
        via_metals = {}

        for net_data in self.config.raw_data.get('target_nets', []):
            net_name = net_data.get('name', 'Unknown')
            safe_name = net_name.replace('/', '').replace('.', '_')
            metal = self.CSX.AddMetal(f'Copper_Vias_Pads_{safe_name}')
            metal.SetColor(self.BASE_COPPER)
            via_metals[net_name] = metal

        all_drill_features = self.config.all_vias + self.config.all_tht_pads
        for feature in all_drill_features:
            net_name = feature.get('net_name', 'Unknown')
            metal = via_metals.get(net_name, fallback_metal)

            vx, vy = to_csx(feature['x'], feature['y'], self.config.center_x, self.config.center_y)
            r = feature.get('size_mm', 0.6) / 2.0

            # --- TOP PRIORITY RULE: Lock mesh lines to the physical edges of the via ---
            self.key_x_points.extend([vx - r, vx + r])
            self.key_y_points.extend([vy - r, vy + r])

            # Inject a guaranteed 3x3 grid internal to the via to form a solid 3D cylinder
            x_cells = self.config.mesh_feature.get("x_cells", 3)
            y_cells = self.config.mesh_feature.get("y_cells", 3)
            if self.mesh_mgr:
                self.mesh_mgr.add_override_region('x', vx - r, vx + r, x_cells, mode='uniform')
                self.mesh_mgr.add_override_region('y', vy - r, vy + r, y_cells, mode='uniform')

            s_layer = self.config.layer_z.get(feature.get('start_layer', 'F.Cu'), {})
            e_layer = self.config.layer_z.get(feature.get('end_layer', 'B.Cu'), {})

            z1 = s_layer.get('z', z_top)
            z2 = e_layer.get('z', z_bottom) - (e_layer.get('thickness', 0.0) if not self.config.is_2_5d else 0.0)

            # The 3D copper barrel (Antipad shell logic safely removed as KiCad natively handles holes)
            metal.AddCylinder([vx, vy, z1], [vx, vy, z2], r, priority=9900)

        # Apply Physical Drill Holes (Hollow Vias & PTH) ---
        if not hasattr(self, 'drill_mat'):
            self.drill_mat = self.CSX.AddMaterial('Air_Drill_Hole', epsilon=1.0, kappa=0.0)
            self.drill_mat.SetColor('#00FFFF', 150)

        for feature in all_drill_features:
            vx, vy = to_csx(feature['x'], feature['y'], self.config.center_x, self.config.center_y)
            pad_size = float(feature.get('size_mm', 0.6))
            drill_value = feature.get('drill_mm', None)

            if drill_value is None:
                drill_dia = pad_size * 0.5
            else:
                drill_dia = float(drill_value)

            if 0.0 < drill_dia < pad_size:
                r_drill = drill_dia / 2.0
                self.key_x_points.extend([vx - r_drill, vx + r_drill])
                self.key_y_points.extend([vy - r_drill, vy + r_drill])

                # Inject a guaranteed internal grid for the air core to prevent 0.0 overwrite artifacts
                x_cells = self.config.mesh_feature.get("x_cells", 3)
                y_cells = self.config.mesh_feature.get("y_cells", 3)
                if self.mesh_mgr:
                    self.mesh_mgr.add_override_region('x', vx - r_drill, vx + r_drill, x_cells, mode='uniform')
                    self.mesh_mgr.add_override_region('y', vy - r_drill, vy + r_drill, y_cells, mode='uniform')

                s_layer = self.config.layer_z.get(feature.get('start_layer', 'F.Cu'), {})
                e_layer = self.config.layer_z.get(feature.get('end_layer', 'B.Cu'), {})
                z1 = s_layer.get('z', z_top)
                z2 = e_layer.get('z', z_bottom) - e_layer.get('thickness', 0.0) if 'z' in e_layer else z_bottom

                self.drill_mat.AddCylinder([vx, vy, z1 + 0.01], [vx, vy, z2 - 0.01], r_drill, priority=9950)

        # --- Explicit Simulation Boundary Ground Plane ---
        bc_z_neg = self.config.raw_data.get("boundary_conditions", {}).get("z_neg", "PML")

        # If the user requested a PEC floor, we MUST draw an infinite metal sheet at the bottom of the stackup
        if "PEC" in bc_z_neg.upper():
            print("[*] Generating Full-Domain PEC Ground Plane to support -Z PEC boundary...")
            z_bottom = min([l['z'] - (l.get('thickness', 0.0) if not self.config.is_2_5d else 0.0)
                            for l in self.config.layer_z.values() if l.get('type') == 'Copper'], default=0.0)
            full_pec = self.CSX.AddMetal('Full_Domain_PEC_GND')
            full_pec.SetColor('#888888')
            full_pec.AddBox(
                    [self.config.board_min_x, self.config.board_min_y, z_bottom],
                    [self.config.board_max_x, self.config.board_max_y, z_bottom],
                    priority=9850
                )
            # Ensure the mesh explicitly snaps to this new reference floor
            self.key_z_points.append(z_bottom)

    def parse_component_value(self, ref, val_str):
        if not val_str or val_str.upper() in ['DNP', 'NM', 'NO STUFF', 'DNI']:
            return 0.0

        val_str = val_str.upper().strip()

        # Handle 4K7 style notation
        match = re.match(r'^([\d]+)([A-Z])([\d]+)$', val_str)
        if match:
            val_str = f"{match.group(1)}.{match.group(3)}{match.group(2)}"

        multipliers = {
            'P': 1e-12, 'N': 1e-9, 'U': 1e-6, 'M': 1e-3,
            'K': 1e3, 'MEG': 1e6, 'G': 1e9
        }

        # Strip standard units
        clean_str = re.sub(r'([A-Z])?[F|H|Ω|OHM|R]+$', r'\1', val_str)

        for mult, factor in multipliers.items():
            if clean_str.endswith(mult):
                try:
                    return float(clean_str[:-len(mult)]) * factor
                except ValueError:
                    return 0.0

        try:
            return float(clean_str)
        except ValueError:
            return 0.0

    def draw_rlc_components(self):
        components = self.config.raw_data.get("discrete_components", [])
        if not components: return

        if not hasattr(self, 'dummy_mat'):
            self.dummy_mat = self.CSX.AddMaterial('Component_Bodies', epsilon=1.0, kappa=0.0)
            self.dummy_mat.SetColor('#333333', 200)

        for comp in components:
            ref = comp['reference']
            val_str = comp['value']
            pads = comp['pads']
            if len(pads) < 2: continue

            px1, py1 = to_csx(pads[0]['x'], pads[0]['y'], self.config.center_x, self.config.center_y)
            px2, py2 = to_csx(pads[1]['x'], pads[1]['y'], self.config.center_x, self.config.center_y)
            ex_dir = 'x' if abs(px2 - px1) > abs(py2 - py1) else 'y'

            cu_layers = [name for name, data in self.config.layer_z.items() if data.get('type') == 'Copper']
            top_cu = cu_layers[0] if cu_layers else 'F.Cu'
            bot_cu = cu_layers[-1] if cu_layers else 'B.Cu'

            raw_layer = str(comp.get('layer', top_cu)).strip()
            layer_name = bot_cu if raw_layer in ['Bottom', 'B.Cu', 'Back', '31'] else top_cu

            if layer_name not in self.config.layer_z:
                layer_name = top_cu

            z_bottom = self.config.layer_z.get(layer_name, {}).get('z', 0.0)
            z_top = z_bottom + 0.6

            min_x, max_x = min(px1, px2) - 0.2, max(px1, px2) + 0.2
            min_y, max_y = min(py1, py2) - 0.2, max(py1, py2) + 0.2

            self.dummy_mat.AddBox([min_x, min_y, z_bottom], [max_x, max_y, z_top], priority=9960)

            parsed_val = self.parse_component_value(ref, val_str)

            # Extract and parse parasitics using the existing robust parser
            p_R_str = comp.get('parasitic_R', '')
            p_L_str = comp.get('parasitic_L', '')
            p_C_str = comp.get('parasitic_C', '')

            p_R = self.parse_component_value('R', p_R_str) if p_R_str else 0.0
            p_L = self.parse_component_value('L', p_L_str) if p_L_str else 0.0
            p_C = self.parse_component_value('C', p_C_str) if p_C_str else 0.0

            w = 0.4 / 2.0

            # Helper to split the gap to create true Series elements in FDTD
            def add_series_elements(elements_list, box_tag):
                n = len(elements_list)
                if n == 0: return
                dx = (px2 - px1) / n
                dy = (py2 - py1) / n

                for i, (etype, evalue) in enumerate(elements_list):
                    sp_x1, sp_y1 = px1 + i * dx, py1 + i * dy
                    sp_x2, sp_y2 = px1 + (i + 1) * dx, py1 + (i + 1) * dy

                    if ex_dir == 'x':
                        b_start, b_stop = [sp_x1, sp_y1 - w, z_bottom], [sp_x2, sp_y2 + w, z_bottom]
                    else:
                        b_start, b_stop = [sp_x1 - w, sp_y1, z_bottom], [sp_x2 + w, sp_y2, z_bottom]

                    lumped = self.CSX.AddLumpedElement(f"Lumped_{ref}_{box_tag}_{etype}_{i}", ny=ex_dir,
                                                       R=evalue if etype == 'R' else 0.0,
                                                       L=evalue if etype == 'L' else 0.0,
                                                       C=evalue if etype == 'C' else 0.0)
                    lumped.AddBox(b_start, b_stop, priority=9970)

            # Topology 1: R and L models (Series R+L, in parallel with C)
            if ref.startswith('R') or ref.startswith('L'):
                main_R = parsed_val if ref.startswith('R') else p_R
                main_L = parsed_val if ref.startswith('L') else p_L
                main_C = p_C

                series_list = []
                if main_R > 0: series_list.append(('R', main_R))
                if main_L > 0: series_list.append(('L', main_L))

                add_series_elements(series_list, "RL_Branch")

                # C is placed in parallel across the entire gap
                if main_C > 0:
                    add_series_elements([('C', main_C)], "C_Branch")

            # Topology 2: C models (R, L, and C all in series)
            elif ref.startswith('C'):
                series_list = []
                if p_R > 0: series_list.append(('R', p_R))
                if p_L > 0: series_list.append(('L', p_L))
                if parsed_val > 0: series_list.append(('C', parsed_val))

                add_series_elements(series_list, "Main_Branch")

            self.key_x_points.extend([px1, px2])
            self.key_y_points.extend([py1, py2])


class MeshManager:
    def __init__(self, config: SimulationConfig):
        self.config = config
        self.overrides = {'x': [], 'y': [], 'z': []}
        self.locked = {'x': set(), 'y': set(), 'z': set()}

    def lock(self, axis, *vals):
        self.locked[axis].update(q(v) for v in vals)

    def add_override_region(self, axis, start_coord, stop_coord, num_cells, mode='uniform'):
        """Registers a specific coordinate region to overwrite the mesh."""
        reg_start = min(start_coord, stop_coord)
        reg_stop = max(start_coord, stop_coord)
        self.overrides[axis].append((reg_start, reg_stop, int(num_cells), mode))

    def _generate_custom_density(self, center_pos, max_distance, min_cell, growth_factor, mode="exponential"):
        """Generates coordinate arrays expanding outwards from a center position."""
        lines = []
        current_offset = 0.0
        current_cell = min_cell

        while current_offset < max_distance:
            # Append positive and negative expansion coordinates
            lines.extend([center_pos + current_offset, center_pos - current_offset])

            # User-defined mathematical density rules
            if mode.lower() == "exponential":
                current_cell *= growth_factor  # Equivalent to min_cell * exp(ratio*step)
            elif mode.lower() == "linear":
                current_cell += growth_factor  # Equivalent to min_cell + ratio*step

            current_offset += current_cell

        return np.array(lines)

    def finalize_mesh(self, CSX, raw_x, raw_y, raw_z):
        grid = CSX.GetGrid()
        grid.SetDeltaUnit(1e-3)

        max_attempts = 5
        attempt = 1
        mesh_resolved = False

        # Iterative solver loop
        while attempt <= max_attempts and not mesh_resolved:
            print(f"[*] Mesh Generation Attempt {attempt}/{max_attempts}...")

            if self.config.mesh_locks.get("conductor", True):
                x_lines = np.unique(np.round(raw_x, 3))
                y_lines = np.unique(np.round(raw_y, 3))
                z_lines = np.unique(np.round(raw_z, 4))
            else:
                x_lines, y_lines, z_lines = np.array([]), np.array([]), np.array([])

            try:
                # Lock all extreme boundary points so they don't get averaged away
                #if len(x_lines) > 0: self.lock('x', min(x_lines), max(x_lines))
                #if len(y_lines) > 0: self.lock('y', min(y_lines), max(y_lines))
                #if len(z_lines) > 0: self.lock('z', *z_lines)

                valid_thk = [l['thickness'] for l in self.config.layer_z.values() if l.get('thickness', 0) > 0]
                z_min_dist = min(min(valid_thk) / 2.0,
                                 self.config.min_cell_size_mm) if valid_thk else self.config.min_cell_size_mm
                xy_min_dist = self.config.min_cell_size_mm

                x_lines = self._merge_locked(x_lines, 'x', xy_min_dist)
                y_lines = self._merge_locked(y_lines, 'y', xy_min_dist)
                z_lines = self._merge_locked(z_lines, 'z', z_min_dist)

                self._auto_detect_features_and_gaps(x_lines, 'x')
                self._auto_detect_features_and_gaps(y_lines, 'y')
                self._auto_detect_features_and_gaps(z_lines, 'z')

                x_lines = self._execute_overrides(x_lines, 'x')
                y_lines = self._execute_overrides(y_lines, 'y')
                z_lines = self._execute_overrides(z_lines, 'z')

                x_lines = self._merge_locked(x_lines, 'x', xy_min_dist)
                y_lines = self._merge_locked(y_lines, 'y', xy_min_dist)
                z_lines = self._merge_locked(z_lines, 'z', z_min_dist)

                # Verification check: Inspect generated arrays for Courant violations
                for axis_lines in [x_lines, y_lines, z_lines]:
                    if len(axis_lines) > 1:
                        min_delta = np.min(np.diff(axis_lines))
                        if min_delta < 0.9e-3:
                            raise ValueError(f"CRITICAL: Sub-micron cell detected ({min_delta:.6f}mm).")

                mesh_resolved = True

            except ValueError as e:
                print(f"[!] Mesh conflict detected on attempt {attempt}: {e}")
                # Relax constraints automatically for the next loop
                self.config.min_cell_size_mm *= 0.8
                for axis in ['x', 'y', 'z']:
                    self.overrides[axis].clear()  # Reset constraints
                print(f"[*] Relaxing min_cell_size_mm to {self.config.min_cell_size_mm:.4f}mm and retrying.")
                attempt += 1

        if not mesh_resolved:
            raise RuntimeError(
                "Mesh requirements are contradictory. Recommendation: Increase min_cell_size_mm or reduce target cells per feature.")

        num_gap_cells = max(3, self.config.mesh_gap.get("z_cells", 4))
        diel_sub_lines = []
        for l_name, l_data in self.config.layer_z.items():
            if l_data.get('type') == 'Dielectric' and l_data.get('thickness', 0.0) > 0.03:
                z_s = l_data.get('start', 0.0)
                z_e = l_data.get('end', 0.0)
                diel_sub_lines.extend(np.linspace(min(z_s, z_e), max(z_s, z_e), num_gap_cells + 1).tolist())

        min_z = min(z_lines) if len(z_lines) > 0 else 0.0
        max_z = max(z_lines) if len(z_lines) > 0 else 0.0
        z_pml_segments = [z_lines, diel_sub_lines]

        # Bottom Air Margin (-Z)
        if self.config.margin_z_neg > 0.001:
            z_air_bottom = np.linspace(min_z - self.config.margin_z_neg, min_z, self.config.pml_cells_count + 1)[:-1]
            z_pml_segments.insert(0, z_air_bottom)

        # Top Air Margin (+Z)
        if self.config.margin_z_pos > 0.001:
            z_air_top = np.linspace(max_z, max_z + self.config.margin_z_pos, self.config.pml_cells_count + 1)[1:]
            z_pml_segments.append(z_air_top)

        # Apply independent padding arrays to X and Y
        pad_x = self._add_padding(x_lines, margin_neg=self.config.margin_x_neg, margin_pos=self.config.margin_x_pos)
        pad_y = self._add_padding(y_lines, margin_neg=self.config.margin_y_neg, margin_pos=self.config.margin_y_pos)

        # Do not overwrite pad_z using _add_padding.
        # Combine the segments we carefully built above!
        pad_z = np.unique(np.round(np.concatenate(z_pml_segments), 4))

        grid.AddLine('x', [float(v) for v in pad_x])
        grid.AddLine('y', [float(v) for v in pad_y])
        grid.AddLine('z', [float(v) for v in pad_z])

        self._apply_global_growth(grid, 'x')
        self._apply_global_growth(grid, 'y')
        if not getattr(self.config, 'is_2_5d', True):
            self._apply_global_growth(grid, 'z')

    def _auto_detect_features_and_gaps(self, lines, axis):
        """Scans geometry lines. Applies the 1/3 Edge Rule for FDTD fringing fields."""
        if len(lines) < 2: return

        for i in range(len(lines) - 1):
            p1, p2 = lines[i], lines[i + 1]
            dist = p2 - p1

            # Do not subdivide if the distance is already close to our safety limit
            if dist <= (self.config.min_cell_size_mm * 1.5):
                continue

            if axis == 'z' and getattr(self.config, 'is_2_5d', True):
                # Z-axis (dielectrics): Keep uniform subdivision for thickness
                num_cells = max(1, self.config.mesh_gap.get("z_cells", 3))
                z_max_size = self.config.mesh_global.get('z', {}).get('max_size', 0.5)
                if (dist / num_cells) > z_max_size:
                    num_cells = int(np.ceil(dist / z_max_size))

                if num_cells > 1:
                    self.add_override_region(axis, p1, p2, num_cells, mode='uniform')
            else:
                # X/Y-axis: We explicitly invoke the Edge Thirds rule between any two geometry boundaries
                self.add_override_region(axis, p1, p2, 0, mode='edge_thirds')

    def _add_padding(self, lines, margin_neg=5.0, margin_pos=5.0):
        if len(lines) == 0: return lines
        min_v, max_v = min(lines), max(lines)

        # 1. Add the free-space margin
        free_space_neg = min_v - margin_neg
        free_space_pos = max_v + margin_pos
        segments = [lines]

        # 2. Build PML OUTSIDE the free space based on the last cell size
        if margin_neg > 0.001:
            segments.insert(0, [free_space_neg])
            pml_step = self.config.min_cell_size_mm * 2.0
            pml_neg = np.linspace(free_space_neg - (pml_step * self.config.pml_cells_count), free_space_neg,
                                  self.config.pml_cells_count + 1)[:-1]
            segments.insert(0, pml_neg)

        if margin_pos > 0.001:
            segments.append([free_space_pos])
            pml_step = self.config.min_cell_size_mm * 2.0
            pml_pos = np.linspace(free_space_pos, free_space_pos + (pml_step * self.config.pml_cells_count),
                                  self.config.pml_cells_count + 1)[1:]
            segments.append(pml_pos)

        return np.unique(np.concatenate(segments))

    def _execute_overrides(self, lines, axis):
        """Applies overrides, using either uniform steps or the 1/3 edge singularity rule."""
        if not self.overrides[axis]:
            return lines

        final_lines = list(lines)

        # Dynamically calculate the required decimal precision based on min_cell_size_mm.
        # e.g., 0.04 mm -> base_digits = 2. 0.005 mm -> base_digits = 3.
        # We add +2 to prevent quantizing original trace coordinates and fractional divisions.
        base_digits = int(np.ceil(-np.log10(self.config.min_cell_size_mm)))
        rounding_digits = max(4, base_digits + 2)

        for (reg_start, reg_stop, num_cells, mode) in self.overrides[axis]:
            # Erase any lines strictly INSIDE the override region
            final_lines = [x for x in final_lines if x <= reg_start or x >= reg_stop]
            dist = reg_stop - reg_start

            if mode == 'uniform':
                override_mesh = np.linspace(reg_start, reg_stop, num_cells + 1)
                final_lines.extend(override_mesh)

            elif mode == 'edge_thirds':
                # The 1/3 Edge Rule
                edge_step = min(dist / 3.0, self.config.min_cell_size_mm * 2.0)
                edge_step = max(edge_step, self.config.min_cell_size_mm)

                # Ensure the middle void is ALSO larger than the minimum cell size
                if dist >= (edge_step * 2.0 + self.config.min_cell_size_mm):
                    # Gap is large enough for distinct edge cells and a safe middle void
                    final_lines.extend([reg_start, reg_start + edge_step, reg_stop - edge_step, reg_stop])
                else:
                    # Gap is too tight. Can we safely split it in half?
                    if (dist / 2.0) >= self.config.min_cell_size_mm:
                        final_lines.extend(np.linspace(reg_start, reg_stop, 3)) # 2 safe segments
                    else:
                        final_lines.extend([reg_start, reg_stop]) # 1 segment (No internal lines)

        # Apply the dynamic rounding
        return np.unique(np.round(final_lines, 3)) #  rounding_digits))

    def _apply_global_growth(self, grid, axis):
        settings = self.config.mesh_global.get(axis, {})
        max_size = settings.get("max_size", 1.0)
        ratio = settings.get("ratio", 1.3)
        growth_type = settings.get("growth", "Exponential")


        # Apply user settings dynamically for all axes (Including Z in 3D)
        if growth_type == "Exponential":
            grid.SmoothMeshLines(axis, max_size, ratio=ratio)
        else:
            # Linear/Conservative growth limit
            grid.SmoothMeshLines(axis, max_size, ratio=min(1.1, ratio))

    def _merge_locked(self, lines, axis, min_dist):
        locked = np.array(sorted(list(self.locked[axis])))
        if locked.size == 0:
            return np.unique(np.round(lines, 3))

        # Keep background lines only if they don't violate the min_dist to a LOCKED port/pad line
        keep = [v for v in lines if np.min(np.abs(locked - v)) > min_dist]
        return np.unique(np.round(np.concatenate([keep, locked]), 3))

    def _filter_close_points(self, points, axis):
        if len(points) == 0:
            return points

        if axis == 'z':
            valid_thk = [l['thickness'] for l in self.config.layer_z.values() if l['thickness'] > 0]
            min_dist = min(min(valid_thk) / 2.0, self.config.min_cell_size_mm) if valid_thk else self.config.min_cell_size_mm
        else:
            min_dist = self.config.min_cell_size_mm

        sorted_coords = np.sort(np.unique(points))
        final_coords = []
        current_cluster = [sorted_coords[0]]

        # Single-Pass Non-Chaining Filter
        for pt in sorted_coords[1:]:
            # Compare against the FIRST point in the cluster, not the LAST
            if (pt - current_cluster[0]) <= min_dist:
                current_cluster.append(pt)
            else:
                final_coords.append(np.mean(current_cluster))
                current_cluster = [pt]

        final_coords.append(np.mean(current_cluster))
        return np.round(final_coords, 4)

    def get_cell_count(self, CSX):
        grid = CSX.GetGrid()
        x, y, z = grid.GetLines('x'), grid.GetLines('y'), grid.GetLines('z')
        nx = max(0, len(x) - 1) if x is not None else 0
        ny = max(0, len(y) - 1) if y is not None else 0
        nz = max(0, len(z) - 1) if z is not None else 0
        return nx * ny * nz, nx, ny, nz


