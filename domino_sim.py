import math
import os
import time

import mujoco
import mujoco.viewer
import numpy as np


# ============================================================
# CONFIGURATION
# ============================================================

TURNS = 10                      # spiral turns (visually = 10 "rings")
INNER_RADIUS = 0.40
SPIRAL_PITCH = 0.085            # radial growth per turn  (> domino width!)
TARGET_ARC_SPACING = 0.060      # centre-to-centre distance along the path

# Domino half-sizes:
#   X = along the path (thickness), Y = across (width), Z = height
HALF_X = 0.01
HALF_Y = 0.03
HALF_Z = 0.06

TIMESTEP = 0.002
START_DELAY = 3.0
MAX_SIM_TIME = 400.0            # SIMULATED seconds, not wall clock
MAX_WALL_TIME = 300.0           # safety cap on real time
STEPS_PER_FRAME = 20            # physics steps per viewer update

INITIAL_TILT = math.radians(30.0)
TOPPLED_THRESHOLD = 0.5         # uprightness < 0.5  =>  fallen (>60 deg)


# ============================================================
# SPIRAL PATH
# ============================================================

def build_spiral_path():
    """
    Archimedean spiral r(theta) = R0 + b*theta, sampled at (almost)
    constant arc length TARGET_ARC_SPACING.

    This is the key fix: instead of 10 independent concentric rings,
    every domino lies on ONE continuous path, so one push reaches
    every single domino.
    """
    b = SPIRAL_PITCH / (2.0 * math.pi)
    r_max = INNER_RADIUS + SPIRAL_PITCH * TURNS

    path = []
    theta = 0.0

    while True:
        r = INNER_RADIUS + b * theta
        if r > r_max:
            break

        x = r * math.cos(theta)
        y = r * math.sin(theta)

        # tangent of the spiral = direction of increasing theta
        dx = b * math.cos(theta) - r * math.sin(theta)
        dy = b * math.sin(theta) + r * math.cos(theta)

        path.append((x, y, math.atan2(dy, dx)))

        # advance by one arc-length step:  ds = sqrt(r^2 + b^2) * dtheta
        theta += TARGET_ARC_SPACING / math.hypot(r, b)

    return path


# ============================================================
# MJCF GENERATION
# ============================================================

def generate_xml(xml_path, path):

    parts = [f"""
<mujoco model="domino_spiral">

    <compiler angle="radian" inertiafromgeom="true"/>

    <option timestep="{TIMESTEP}"
            integrator="implicitfast"
            gravity="0 0 -9.81"
            cone="elliptic"/>

    <default>
        <joint damping="0.02"/>
        <geom friction="1.0 0.02 0.001"
              density="500"
              condim="4"/>
    </default>

    <visual>
        <global offwidth="1280" offheight="800"/>
    </visual>

    <worldbody>

        <light pos="0 0 4" dir="0 0 -1"/>
        <light pos="3 3 3" dir="-1 -1 -1"/>

        <geom name="ground"
              type="plane"
              size="6 6 0.1"
              pos="0 0 0"
              rgba="0.85 0.85 0.85 1"/>
"""]

    names = []

    for i, (x, y, yaw) in enumerate(path):

        name = f"d{i:04d}"
        names.append(name)

        parts.append(f"""
        <body name="{name}"
              pos="{x:.6f} {y:.6f} {HALF_Z:.6f}"
              euler="0 0 {yaw:.8f}">

            <freejoint name="j_{name}"/>

            <geom name="g_{name}"
                  type="box"
                  size="{HALF_X} {HALF_Y} {HALF_Z}"
                  rgba="0.2 0.5 0.9 1"/>

        </body>
""")

    parts.append("""
    </worldbody>
</mujoco>
""")

    with open(xml_path, "w", encoding="utf-8") as file:
        file.write("".join(parts))

    return names


# ============================================================
# HELPERS
# ============================================================

def get_uprightness(data, body_id):
    rotation = data.xmat[body_id].reshape(3, 3)
    vertical_axis = rotation[:, 2]
    return float(np.dot(vertical_axis, [0.0, 0.0, 1.0]))


def count_toppled(data, body_ids):
    total = 0
    for body_id in body_ids:
        if get_uprightness(data, body_id) < TOPPLED_THRESHOLD:
            total += 1
    return total


# ============================================================
# INITIAL TILT  (fall direction = +local X = along the chain)
# ============================================================

def tilt_first_domino(model, data, joint_id):

    qadr = model.jnt_qposadr[joint_id]

    base_q = data.qpos[qadr + 3:qadr + 7].copy()

    half = INITIAL_TILT / 2.0
    tilt_q = np.array([math.cos(half), 0.0, math.sin(half), 0.0])

    w1, x1, y1, z1 = base_q
    w2, x2, y2, z2 = tilt_q

    result = np.array([
        w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2,
        w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
        w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
        w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2,
    ])

    data.qpos[qadr + 3:qadr + 7] = result

    dof = model.jnt_dofadr[joint_id]
    data.qvel[dof:dof + 6] = 0.0

    mujoco.mj_forward(model, data)

    print(f"First domino tilted by {math.degrees(INITIAL_TILT):.1f} degrees.")
    print("Initial velocity is zero.")


# ============================================================
# MAIN
# ============================================================

def main():

    script_dir = os.path.dirname(os.path.abspath(__file__))
    xml_path = os.path.join(script_dir, "domino.xml")

    path = build_spiral_path()
    names = generate_xml(xml_path, path)

    model = mujoco.MjModel.from_xml_path(xml_path)
    model.opt.timestep = TIMESTEP

    data = mujoco.MjData(model)

    # Body ids, in chain order
    body_ids = []
    for name in names:
        body_id = mujoco.mj_name2id(
            model, mujoco.mjtObj.mjOBJ_BODY, name
        )
        if body_id < 0:
            raise RuntimeError(f"Could not find body: {name}")
        body_ids.append(body_id)

    # First domino's joint
    first_joint_id = mujoco.mj_name2id(
        model, mujoco.mjtObj.mjOBJ_JOINT, f"j_{names[0]}"
    )
    if first_joint_id < 0:
        raise RuntimeError("Could not find the first domino joint.")

    data.qvel[:] = 0.0
    mujoco.mj_forward(model, data)

    total_dominoes = len(names)

    # dominoes per turn (for the printout only)
    counts_per_turn = [0] * TURNS
    for i, (x, y, yaw) in enumerate(path):
        theta = math.atan2(y, x) if i == 0 else None  # placeholder
    # simple estimate using radius
    for (x, y, _) in path:
        r = math.hypot(x, y)
        turn = int(round((r - INNER_RADIUS) / SPIRAL_PITCH))
        turn = max(0, min(TURNS - 1, turn))
        counts_per_turn[turn] += 1

    print("=" * 45)
    print("DOMINO SPIRAL SIMULATION")
    print("=" * 45)
    print(f"Turns (rings): {TURNS}")
    print(f"Total dominoes: {total_dominoes}")
    print(f"Dominoes per turn (approx.): {counts_per_turn}")
    print("Opening viewer...")

    # --------------------------------------------------------
    # VIEWER
    # --------------------------------------------------------

    with mujoco.viewer.launch_passive(model, data) as viewer:

        viewer.cam.azimuth = 90
        viewer.cam.elevation = -35
        viewer.cam.distance = 3.6
        viewer.cam.lookat[:] = [0.0, 0.0, 0.1]

        print(f"Waiting {START_DELAY:.0f} seconds...")
        wait_start = time.monotonic()
        while (viewer.is_running()
               and time.monotonic() - wait_start < START_DELAY):
            viewer.sync()
            time.sleep(0.01)

        if not viewer.is_running():
            return

        # ----------------------------------------------------
        # TRIGGER
        # ----------------------------------------------------

        print("Triggering first domino...")
        tilt_first_domino(model, data, first_joint_id)
        viewer.sync()

        # ----------------------------------------------------
        # SIMULATION LOOP
        # ----------------------------------------------------

        print("Simulation started.")
        wall_start = time.monotonic()
        last_report = 0.0

        while viewer.is_running():

            # run physics faster than real time
            for _ in range(STEPS_PER_FRAME):
                mujoco.mj_step(model, data)

            viewer.sync()

            toppled = count_toppled(data, body_ids)

            if data.time - last_report > 5.0:
                last_report = data.time
                print(f"  t = {data.time:6.1f} s   "
                      f"toppled {toppled:4d} / {total_dominoes}")

            if toppled == total_dominoes:
                print("All dominoes have toppled!")
                break

            if data.time > MAX_SIM_TIME:
                print("Reached MAX_SIM_TIME.")
                break

            if time.monotonic() - wall_start > MAX_WALL_TIME:
                print("Reached MAX_WALL_TIME.")
                break

        # ----------------------------------------------------
        # RESULTS
        # ----------------------------------------------------

        toppled = count_toppled(data, body_ids)

        print()
        print("=" * 45)
        print("SIMULATION RESULTS")
        print("=" * 45)
        print(f"Simulated time: {data.time:.1f} s")
        print(f"Toppled:        {toppled} / {total_dominoes}")
        print(f"Still upright:  {total_dominoes - toppled}")

        if toppled == total_dominoes:
            print("SUCCESS: 100% of the dominoes toppled.")
        else:
            print("Chain did not complete.")

        while viewer.is_running():
            viewer.sync()
            time.sleep(0.01)


if __name__ == "__main__":
    main()