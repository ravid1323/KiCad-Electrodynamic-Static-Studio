import os
from CSXCAD import AppCSXCAD_BIN
from CSXCAD.CSXCAD import ContinuousStructure
import numpy as np

dll_path = r'C:\openEMS'
if os.path.exists(dll_path):
    os.add_dll_directory(dll_path)
    os.environ['PATH'] = dll_path + os.pathsep + os.environ.get('PATH', '')

class PropertyWrapper:
    """Wraps CSXCAD properties (Metal, Material, etc.) to log geometry primitives."""
    def __init__(self, real_prop, record_dict):
        self._prop = real_prop
        self._record = record_dict

    def __getattr__(self, name):
        return getattr(self._prop, name)

    # --- GEOMETRY PRIMITIVE INTERCEPTS ---
    def AddBox(self, start, stop, **kw):
        prim = {"type": "box", "start": start, "stop": stop, "kwargs": kw}
        self._record["primitives"].append(prim)
        return self._prop.AddBox(start, stop, **kw)

    def AddCylinder(self, start, stop, radius, **kw):
        prim = {"type": "cylinder", "start": start, "stop": stop, "radius": radius, "kwargs": kw}
        self._record["primitives"].append(prim)
        return self._prop.AddCylinder(start, stop, radius, **kw)

    def AddLinPoly(self, points, norm_dir, elevation, length, **kw):
        prim = {"type": "linpoly", "points": points, "norm_dir": norm_dir,
                "elevation": elevation, "length": length, "kwargs": kw}
        self._record["primitives"].append(prim)
        return self._prop.AddLinPoly(points, norm_dir, elevation, length, **kw)

    def AddCurve(self, points, **kw):
        prim = {"type": "curve", "points": points, "kwargs": kw}
        self._record["primitives"].append(prim)
        return self._prop.AddCurve(points, **kw)

    def AddCylindricalShell(self, start, stop, radius, shell_width, **kw):
        prim = {"type": "cylindricalshell", "start": start, "stop": stop,
                "radius": radius, "shell_width": shell_width, "kwargs": kw}
        self._record["primitives"].append(prim)
        return self._prop.AddCylindricalShell(start, stop, radius, shell_width, **kw)

    def AddPoint(self, coord, **kw):
        prim = {"type": "point", "coord": coord, "kwargs": kw}
        self._record["primitives"].append(prim)
        return self._prop.AddPoint(coord, **kw)

    def AddPolygon(self, points, norm_dir, elevation, **kw):
        prim = {"type": "polygon", "points": points, "norm_dir": norm_dir, "elevation": elevation, "kwargs": kw}
        self._record["primitives"].append(prim)
        return self._prop.AddPolygon(points, norm_dir, elevation, **kw)

    def AddPolyhedron(self, **kw):
        prim = {"type": "polyhedron", "kwargs": kw, "vertices": [], "faces": []}
        self._record["primitives"].append(prim)
        return self._prop.AddPolyhedron(**kw)

    def AddPolyhedronReader(self, filename, **kw):
        prim = {"type": "polyhedronreader", "filename": filename, "kwargs": kw}
        self._record["primitives"].append(prim)
        return self._prop.AddPolyhedronReader(filename, **kw)

    def AddRotPoly(self, points, norm_dir, elevation, rot_axis, angle, **kw):
        prim = {"type": "rotpoly", "points": points, "norm_dir": norm_dir,
                "elevation": elevation, "rot_axis": rot_axis, "angle": angle, "kwargs": kw}
        self._record["primitives"].append(prim)
        return self._prop.AddRotPoly(points, norm_dir, elevation, rot_axis, angle, **kw)

    def AddSphere(self, center, radius, **kw):
        prim = {"type": "sphere", "center": center, "radius": radius, "kwargs": kw}
        self._record["primitives"].append(prim)
        return self._prop.AddSphere(center, radius, **kw)

    def AddSphericalShell(self, center, radius, shell_width, **kw):
        prim = {"type": "sphericalshell", "center": center, "radius": radius, "shell_width": shell_width, "kwargs": kw}
        self._record["primitives"].append(prim)
        return self._prop.AddSphericalShell(center, radius, shell_width, **kw)

    def AddWire(self, points, radius, **kw):
        prim = {"type": "wire", "points": points, "radius": radius, "kwargs": kw}
        self._record["primitives"].append(prim)
        return self._prop.AddWire(points, radius, **kw)

    # --- GET QUERIES ---
    def GetAllPrimitives(self):
        return self._record.get("primitives", [])

    def GetName(self):
        return self._record.get("name")

    def GetTypeString(self):
        return self._record.get("type")


class CSXGeometryLogger:
    """Wraps CSXCAD.ContinuousStructure to intercept and log property definitions."""
    def __init__(self, csx_engine: ContinuousStructure):
        self.engine = csx_engine
        self.properties = []

    def __getattr__(self, name):
        return getattr(self.engine, name)

    def GetCSX(self):
        return self.engine

    def Write2XML(self, path):
        self.engine.Write2XML(path)

    def reset(self):
        """Clears all logged properties and primitives to prevent duplication."""
        self.properties.clear()
        # If the underlying continuous structure has a clear method, invoke it here
        if hasattr(self.engine, "Clear"):
            self.engine.Clear()

    def check_primitive_cell_overlap(self, cell_min, cell_max, primitive_obj, eps=1e-9):
        """
        Conservative broad-phase overlap test.

        Returns True when the primitive's axis-aligned bounding box
        overlaps the cell bounding box in X, Y, AND Z.

        Boundary touching counts as overlap within `eps`.

        This does NOT guarantee that the actual primitive geometry
        intersects the cell; it only guarantees that their AABBs intersect.
        """

        def aabb_intersect(min1, max1, min2, max2):
            min1 = np.asarray(min1, dtype=float)
            max1 = np.asarray(max1, dtype=float)
            min2 = np.asarray(min2, dtype=float)
            max2 = np.asarray(max2, dtype=float)

            # Normalize AABBs in case coordinates were supplied reversed.
            box1_min = np.minimum(min1, max1)
            box1_max = np.maximum(min1, max1)
            box2_min = np.minimum(min2, max2)
            box2_max = np.maximum(min2, max2)

            return bool(np.all(
                (box1_min <= box2_max + eps) &
                (box1_max >= box2_min - eps)
            ))

        tag = str(primitive_obj.get("type", "")).lower()

        # ------------------------------------------------------------
        # BOX
        # ------------------------------------------------------------
        if tag == "box":
            p1 = primitive_obj.get("start")
            p2 = primitive_obj.get("stop")

            if p1 is None or p2 is None:
                return False

            return aabb_intersect(cell_min, cell_max, p1, p2)

        # ------------------------------------------------------------
        # CYLINDER
        # ------------------------------------------------------------
        elif tag == "cylinder":
            p1 = primitive_obj.get("start")
            p2 = primitive_obj.get("stop")
            r = primitive_obj.get("radius")

            if p1 is None or p2 is None or r is None:
                return False

            p1 = np.asarray(p1, dtype=float)
            p2 = np.asarray(p2, dtype=float)
            r = float(r)

            cyl_min = np.minimum(p1, p2) - r
            cyl_max = np.maximum(p1, p2) + r

            return aabb_intersect(cell_min, cell_max, cyl_min, cyl_max)

        # ------------------------------------------------------------
        # POLYGON / LINPOLY
        # ------------------------------------------------------------
        elif tag in ("polygon", "linpoly"):
            pts_x, pts_y = primitive_obj.get("points", ([], []))

            if len(pts_x) == 0 or len(pts_y) == 0:
                return False

            pts_x = np.asarray(pts_x, dtype=float)
            pts_y = np.asarray(pts_y, dtype=float)

            z_elev = float(primitive_obj.get("elevation", 0.0))
            length = float(primitive_obj.get("length", 0.0)) if tag == "linpoly" else 0.0

            norm_dir = int(
                primitive_obj.get(
                    "norm_dir",
                    primitive_obj.get("normdir", 2)
                )
            )

            a = min(pts_x)
            b = max(pts_x)
            c = min(pts_y)
            d = max(pts_y)

            extrude_min = min(z_elev, z_elev + length)
            extrude_max = max(z_elev, z_elev + length)

            if norm_dir == 2:  # XY + Z
                prim_min = [a, c, extrude_min]
                prim_max = [b, d, extrude_max]

            elif norm_dir == 1:  # XZ + Y
                prim_min = [a, extrude_min, c]
                prim_max = [b, extrude_max, d]

            elif norm_dir == 0:  # YZ + X
                prim_min = [extrude_min, a, c]
                prim_max = [extrude_max, b, d]

            else:
                return False

            return aabb_intersect(
                cell_min,
                cell_max,
                prim_min,
                prim_max
            )

        # ------------------------------------------------------------
        # SPHERE / SPHERICAL SHELL
        # ------------------------------------------------------------
        elif tag in ("sphere", "sphericalshell"):
            center = primitive_obj.get("center")
            r = primitive_obj.get("radius")

            if center is None or r is None:
                return False

            center = np.asarray(center, dtype=float)
            r = float(r)

            prim_min = center - r
            prim_max = center + r

            return aabb_intersect(
                cell_min,
                cell_max,
                prim_min,
                prim_max
            )

        return False
    # --- PROPERTY INTERCEPTS ---
    def AddMetal(self, name, **kw):
        mat = self.engine.AddMetal(name)
        record = {"name": name, "type": "metal", "kwargs": kw, "primitives": []}
        self.properties.append(record)
        return PropertyWrapper(mat, record)

    def AddMaterial(self, name, **kw):
        mat = self.engine.AddMaterial(name, **kw)
        record = {"name": name, "type": "material", "kwargs": kw, "primitives": []}
        self.properties.append(record)
        return PropertyWrapper(mat, record)

    def AddConductingSheet(self, name, **kw):
        mat = self.engine.AddConductingSheet(name, **kw)
        record = {"name": name, "type": "conducting_sheet", "kwargs": kw, "primitives": []}
        self.properties.append(record)
        return PropertyWrapper(mat, record)

    def AddLumpedElement(self, name, **kw):
        mat = self.engine.AddLumpedElement(name, **kw)
        record = {"name": name, "type": "lumped_element", "kwargs": kw, "primitives": []}
        self.properties.append(record)
        return PropertyWrapper(mat, record)

    def AddExcitation(self, name, exc_type, exc_val, **kw):
        mat = self.engine.AddExcitation(name, exc_type, exc_val, **kw)
        record = {"name": name, "type": "excitation", "exc_type": exc_type, "exc_val": exc_val, "kwargs": kw, "primitives": []}
        self.properties.append(record)
        return PropertyWrapper(mat, record)

    def AddDump(self, name, **kw):
        mat = self.engine.AddDump(name, **kw)
        record = {"name": name, "type": "dump", "kwargs": kw, "primitives": []}
        self.properties.append(record)
        return PropertyWrapper(mat, record)

    def AddProbe(self, name, p_type, **kw):
        mat = self.engine.AddProbe(name, p_type, **kw)
        record = {"name": name, "type": "probe", "p_type": p_type, "kwargs": kw, "primitives": []}
        self.properties.append(record)
        return PropertyWrapper(mat, record)

    # --- GET QUERIES ---
    def GetAllProperties(self):
        return self.properties

    def GetPropertiesByName(self, name):
        return [p for p in self.properties if p.get("name") == name]

    def GetPropertyByType(self, prop_type):
        return [p for p in self.properties if p.get("type") == prop_type]

    def GetVoxelizationMaterials(self):
        """
        Pre-processes all physical materials for the C++ voxelizer.
        Automatically handles PEC fallbacks, priority sorting, and property extraction per primitive.
        """
        valid_types = ["metal", "material", "conducting_sheet"]
        voxel_objects = []

        for prop in self.properties:
            if prop["type"] not in valid_types:
                continue

            # Extract base material properties
            kw = prop.get("kwargs", {})
            sigma = float(kw.get("kappa", kw.get("conductivity", 0.0)))
            eps = float(kw.get("epsilon", 1.0))
            mu = float(kw.get("mue", 1.0))
            thickness = float(kw.get("thickness", 0.0))

            if prop["type"] == "metal" and sigma == 0.0:
                sigma = 1e10  # PEC fallback

            # Extract priority per primitive and flatten the list
            for prim in prop["primitives"]:
                prim_kw = prim.get("kwargs", {})
                priority = int(prim_kw.get("priority", 0))

                voxel_objects.append({
                    "material_name": prop["name"],
                    "priority": priority,
                    "sigma": sigma,
                    "eps": eps,
                    "mu": mu,
                    "thickness": thickness,
                    "primitive_data": prim
                })

        # Sort individual geometric objects by priority ascending
        # Higher priorities will overwrite lower ones during the grid mapping
        voxel_objects.sort(key=lambda x: x["priority"])
        return voxel_objects