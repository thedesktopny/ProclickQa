"""
Skin Block detection engine — the SERVER-side path.

This is the same algorithm the browser runs, so a photo comes out identical
whether it was processed on the agent's PC or here. Deliberately kept in step
with browser_engine.js: if you change one, change the other.

How it works
------------
1. SegFormer human-parsing (18 classes) scores every pixel. We take the best
   SKIN score minus the best non-skin score — a continuous number, not a
   yes/no label — and the best CLOTHING score the same way.
2. Both score maps are upsampled smoothly and compared per pixel, so the line
   between skin and a bikini strap lands on the real edge instead of a blocky
   staircase.
3. The model has NO torso class: a bare midriff comes back labelled
   "Upper-clothes". So we learn the person's own skin colour from the skin the
   model did find (face, arms) and grow into matching neighbours, stopping at
   real image edges — a bra or bikini blocks it, bare skin does not.
4. Each covered area is filled with its own averaged tone so the patch sits in
   the picture rather than being a black hole.

Speed: one model run is timed on first use and the number of windows is chosen
to fit the time budget, because Azure aborts any request at ~230 seconds.
"""
import os
import time
import urllib.request

import numpy as np
import cv2

MODEL_DIR = os.path.join(os.getenv('HOME', '.'), 'sbmodel')
MODEL_PATH = os.path.join(MODEL_DIR, 'segformer_b2_clothes.onnx')
MODEL_URLS = [
    'https://huggingface.co/Xenova/segformer_b2_clothes/resolve/main/onnx/model_quantized.onnx',
    'https://huggingface.co/Xenova/segformer_b2_clothes/resolve/main/onnx/model.onnx',
]

# class ids
BACKGROUND, HAT, HAIR, SUNGLASSES, UPPER, SKIRT, PANTS, DRESS, BELT = 0, 1, 2, 3, 4, 5, 6, 7, 8
LEFT_SHOE, RIGHT_SHOE, FACE, LEFT_LEG, RIGHT_LEG, LEFT_ARM, RIGHT_ARM, BAG, SCARF = 9, 10, 11, 12, 13, 14, 15, 16, 17
SKIN_CLASSES = [FACE, LEFT_LEG, RIGHT_LEG, LEFT_ARM, RIGHT_ARM]
HEAD_CLASSES = [HAIR, HAT]
CLOTH_CLASSES = [UPPER, SKIRT, PANTS, DRESS, BELT, LEFT_SHOE, RIGHT_SHOE, BAG, SUNGLASSES]

# tunables (all overridable from Skin Block settings)
COVER_HAIR = False
COVER_MODE = 'blend'
SKIN_BIAS = 0.35
SMOOTH_PX = 2
EXTEND_BY_COLOUR = True
COLOUR_TOLERANCE = 12.0
COARSE_ENOUGH = 0.28    # subject taller than this share of the frame -> one pass is enough
MAX_GROW_PX = 90        # how far growth may travel from confirmed skin
MAX_GROW_RATIO = 2.4    # throw the growth away if it balloons past this
SECOND_PASS = True
WINDOW_PX = 260
MAX_WINDOWS = 60
TIME_BUDGET = 110.0

INPUT_SIZE = 512        # detail windows: small thumbnails genuinely need this
COARSE_SIZE = 384       # the first look — and the whole answer when one big
                        # subject fills the frame, where 512 buys nothing visible
MEAN = np.array([0.485, 0.456, 0.406], np.float32)
STD = np.array([0.229, 0.224, 0.225], np.float32)

_session = None
_session_tried = False
_model_error = ''
_speed = None


def _looks_like_model(path):
    try:
        if os.path.getsize(path) < 1024 * 1024:
            return False
        with open(path, 'rb') as f:
            head = f.read(32).lstrip()
        for bad in (b'/*', b'<!', b'<htm', b'{', b'var ', b'version http'):
            if head[:len(bad)].lower() == bad.lower():
                return False
        return True
    except Exception:
        return False


def _download_model():
    os.makedirs(MODEL_DIR, exist_ok=True)
    last = ''
    for url in MODEL_URLS:
        try:
            tmp = MODEL_PATH + '.part'
            req = urllib.request.Request(url, headers={'User-Agent': 'VoiceGuard-SkinBlock/1.0'})
            with urllib.request.urlopen(req, timeout=600) as resp, open(tmp, 'wb') as f:
                while True:
                    chunk = resp.read(1024 * 512)
                    if not chunk:
                        break
                    f.write(chunk)
            if not _looks_like_model(tmp):
                os.remove(tmp)
                last = 'downloaded file is not a model'
                continue
            os.replace(tmp, MODEL_PATH)
            return True
        except Exception as e:
            last = str(e)[:200]
    raise RuntimeError(last or 'download failed')


def get_session():
    global _session, _session_tried, _model_error
    if _session is not None or _session_tried:
        return _session
    _session_tried = True
    try:
        import onnxruntime as ort
        if not os.path.exists(MODEL_PATH) or not _looks_like_model(MODEL_PATH):
            _download_model()
        so = ort.SessionOptions()
        so.intra_op_num_threads = max(1, (os.cpu_count() or 2))
        _session = ort.InferenceSession(MODEL_PATH, so, providers=['CPUExecutionProvider'])
        print('[skinblock] parser model loaded')
    except Exception as e:
        _model_error = str(e)[:200]
        print('[skinblock] parser unavailable: ' + _model_error)
        _session = None
    return _session


def model_status():
    return {'model': bool(get_session()), 'error': _model_error}


def _auto_levels(img):
    """Dark thumbnails read as background to the model. Brighten the INPUT only."""
    mean = float(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).mean())
    if mean <= 4 or mean >= 105:
        return img
    gamma = max(0.35, min(1.0, np.log(0.42) / np.log(mean / 255.0)))
    lut = np.array([round(255 * (i / 255.0) ** gamma) for i in range(256)], np.uint8)
    return cv2.LUT(img, lut)


def _scores(img, x0, y0, x1, y1):
    """Returns (skin_score, cloth_score) grids for one window."""
    sess = get_session()
    crop = cv2.resize(img[y0:y1, x0:x1], (INPUT_SIZE, INPUT_SIZE), interpolation=cv2.INTER_LINEAR)
    crop = _auto_levels(crop)
    rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    x = ((rgb - MEAN) / STD).transpose(2, 0, 1)[None].astype(np.float32)
    lg = sess.run(None, {sess.get_inputs()[0].name: x})[0][0]
    skin_ids = list(SKIN_CLASSES) + (list(HEAD_CLASSES) if COVER_HAIR else [])
    skin_ids = [k for k in skin_ids if k < lg.shape[0]]
    other_ids = [k for k in range(lg.shape[0]) if k not in skin_ids]
    cloth_ids = [k for k in CLOTH_CLASSES if k < lg.shape[0] and k not in skin_ids]
    skin = lg[skin_ids].max(axis=0)
    other = lg[other_ids].max(axis=0)
    cloth = lg[cloth_ids].max(axis=0)
    return skin - other + SKIN_BIAS, cloth - skin


def _extend_by_colour(img, mask):
    """Reclaim bare torso the model labels as clothing.

    Runs per person, not per window. Sampling colour across a whole window mixed
    several thumbnails together, producing an average that matched almost
    anything and then spread up to 40px — which is how clothing got swallowed.
    Now: each connected patch of confirmed skin learns its OWN colour, growth is
    confined to that patch's neighbourhood, capped in distance, and thrown away
    entirely if it balloons past a sane multiple of what it started from.
    """
    n, lab, stats, _ = cv2.connectedComponentsWithStats(mask)
    if n <= 1:
        return mask
    out = mask.copy()
    H, W = mask.shape[:2]
    for i in range(1, n):
        area = int(stats[i, cv2.CC_STAT_AREA])
        if area < 150:                       # too small to trust as a colour sample
            continue
        x, y, bw, bh = (int(stats[i, cv2.CC_STAT_LEFT]), int(stats[i, cv2.CC_STAT_TOP]),
                        int(stats[i, cv2.CC_STAT_WIDTH]), int(stats[i, cv2.CC_STAT_HEIGHT]))
        reach = int(min(MAX_GROW_PX, max(12, 0.9 * max(bw, bh))))
        x0, y0 = max(0, x - reach), max(0, y - reach)
        x1, y1 = min(W, x + bw + reach), min(H, y + bh + reach)
        sub = img[y0:y1, x0:x1]
        seed = (lab[y0:y1, x0:x1] == i).astype(np.uint8)
        if int(seed.sum()) < 150:
            continue

        lab_img = cv2.cvtColor(sub, cv2.COLOR_BGR2LAB).astype(np.float32)
        med = np.median(lab_img[seed > 0].reshape(-1, 3), axis=0)
        d = np.sqrt(((lab_img[:, :, 1:] - med[1:]) ** 2).sum(axis=2))
        dl = np.abs(lab_img[:, :, 0] - med[0])
        allowed = ((d < COLOUR_TOLERANCE) & (dl < 45)).astype(np.uint8)
        edges = cv2.dilate(cv2.Canny(cv2.GaussianBlur(sub, (3, 3), 0), 40, 110),
                           np.ones((2, 2), np.uint8))
        allowed[edges > 0] = 0
        allowed[seed > 0] = 1

        grown = seed.copy()
        k = np.ones((3, 3), np.uint8)
        prev = -1
        for _ in range(reach):               # travel is bounded by reach, not a fixed 40
            grown = cv2.dilate(grown, k) & allowed
            s = int(grown.sum())
            if s == prev:
                break
            prev = s
        # runaway guard: bare torso roughly doubles a patch; a flood does far more
        if int(grown.sum()) > area * MAX_GROW_RATIO:
            continue
        out[y0:y1, x0:x1] |= grown
    return out


def _measure_speed(img):
    """One timed run so the window count fits this server's actual speed."""
    global _speed
    if _speed is not None:
        return _speed
    probe = cv2.resize(img, (INPUT_SIZE, INPUT_SIZE), interpolation=cv2.INTER_AREA)
    t = time.time()
    _scores(probe, 0, 0, INPUT_SIZE, INPUT_SIZE)
    _speed = max(0.05, time.time() - t)
    print('[skinblock] one model run takes %.1fs on this server' % _speed)
    return _speed


def _paint(out, img, mask, cover):
    if not mask.any():
        return
    if COVER_MODE != 'blend':
        out[mask > 0] = cover
        return
    n, lab, stats, _ = cv2.connectedComponentsWithStats(mask)
    for i in range(1, n):
        sel = lab == i
        if stats[i, cv2.CC_STAT_AREA] < 20:
            continue
        tone = np.clip(img[sel].reshape(-1, 3).mean(axis=0) * 0.92 + 14, 0, 255).astype(np.uint8)
        out[sel] = tone


def process(img, cover=(0, 0, 0), grid_face_threshold=4, include_neck=False):
    """Paint skin. Returns (painted_image, info)."""
    t0 = time.time()
    h, w = img.shape[:2]
    out = img.copy()
    info = {'detector': 'model', 'painted': 0, 'partial': False, 'windows': 0}

    if get_session() is None:
        info['detector'] = 'unavailable'
        info['wash'] = True
        info['seconds'] = round(time.time() - t0, 1)
        return out, info

    # ---- one coarse look first ----------------------------------------
    # Windows exist so that small thumbnails in a screenshot get enough
    # resolution. When a photo holds one big person, the whole frame at model
    # size is already plenty — so look once, and only spend more runs where the
    # people are actually small. This is the difference between 1 run and 25 on
    # an ordinary product photo.
    _full = INPUT_SIZE
    globals()['INPUT_SIZE'] = COARSE_SIZE
    cS, cC = _scores(img, 0, 0, w, h)
    globals()['INPUT_SIZE'] = _full
    bigS = cv2.resize(cS, (w, h), interpolation=cv2.INTER_CUBIC)
    bigC = cv2.resize(cC, (w, h), interpolation=cv2.INTER_CUBIC)
    coarse = ((bigS > 0) & (bigC <= 0)).astype(np.uint8)
    # A separate, deliberately generous map for deciding which windows to skip.
    # The first look runs small and cannot resolve tiny thumbnails, so anything
    # even faintly person-like keeps its window — skipping is only for areas
    # that are clearly empty.
    gate = (bigS > -2.0).astype(np.uint8)
    gate = cv2.dilate(gate, np.ones((41, 41), np.uint8))
    info['windows'] = 1
    # Subject HEIGHT separates the two cases far more cleanly than area:
    # a portrait's largest skin patch runs ~40% of the frame height, while the
    # biggest face in a grid of thumbnails is ~14%.
    tallest = 0
    if coarse.any():
        nn, _, st, _ = cv2.connectedComponentsWithStats(coarse)
        if nn > 1:
            tallest = int(st[1:, cv2.CC_STAT_HEIGHT].max())
    big_enough = tallest > h * COARSE_ENOUGH
    if big_enough:
        print('[skinblock] one large subject — a single pass is enough')
        mask = coarse
        if EXTEND_BY_COLOUR:
            mask = _extend_by_colour(img, mask)
        if SMOOTH_PX > 0:
            k = int(2 * SMOOTH_PX + 1)
            sm = (cv2.blur(mask.astype(np.float32), (k, k)) > 0.45).astype(np.uint8)
            bigC = cv2.resize(cC, (w, h), interpolation=cv2.INTER_CUBIC)
            sm[(bigC > 0) & (mask == 0)] = 0
            mask = sm
        _paint(out, img, mask, cover)
        info['painted'] = int(mask.any())
        info['wash'] = not bool(mask.any())
        info['seconds'] = round(time.time() - t0, 1)
        return out, info

    per_run = _measure_speed(img)
    passes = [(0, 0), (0.5, 0.5)] if SECOND_PASS else [(0, 0)]
    # Window size is decided by the CONTENT, not by how fast this machine is.
    # Letting a time budget shrink the grid meant a slow server produced worse
    # covering than a fast one — windows spanning several thumbnails can't
    # resolve a bikini strap. The budget still stops a runaway request, but it
    # no longer quietly lowers quality.
    cols = max(1, int(round(w / float(WINDOW_PX))))
    rows = max(1, int(round(h / float(WINDOW_PX))))
    limit = MAX_WINDOWS
    while cols * rows > limit and (cols > 1 or rows > 1):
        if cols >= rows and cols > 1:
            cols -= 1
        elif rows > 1:
            rows -= 1
        else:
            break
    print('[skinblock] %dx%d windows x%d passes (~%.0fs of %.0fs budget)'
          % (cols, rows, len(passes), cols * rows * len(passes) * per_run, TIME_BUDGET))

    # Start the detail pass from scratch. The coarse look is deliberately
    # low-resolution — it can't resolve a bikini strap — and because scores are
    # combined by taking the maximum, letting it into the final map would
    # override what the detail windows correctly saw. It is used ONLY to decide
    # whether detail is needed at all, and where to spend it.
    S = np.full((h, w), -9.0, np.float32)
    C = np.full((h, w), -9.0, np.float32)
    cw, ch = w / float(cols), h / float(rows)
    OV = 0.12
    boxes = []
    started = time.time()
    partial = False
    for ox, oy in passes:
        for ry in range(-1 if oy else 0, rows):
            for rx in range(-1 if ox else 0, cols):
                if time.time() - started > TIME_BUDGET - per_run * 0.5:
                    partial = True
                    break
                x0 = max(0, int((rx + ox) * cw - cw * OV))
                y0 = max(0, int((ry + oy) * ch - ch * OV))
                x1 = min(w, int((rx + 1 + ox) * cw + cw * OV))
                y1 = min(h, int((ry + 1 + oy) * ch + ch * OV))
                if x1 - x0 < 16 or y1 - y0 < 16:
                    continue
                if not gate[y0:y1, x0:x1].any():
                    continue          # clearly empty — don't spend a run on it
                ss, cs = _scores(img, x0, y0, x1, y1)
                sw, sh = x1 - x0, y1 - y0
                np.maximum(S[y0:y1, x0:x1],
                           cv2.resize(ss, (sw, sh), interpolation=cv2.INTER_CUBIC),
                           out=S[y0:y1, x0:x1])
                np.maximum(C[y0:y1, x0:x1],
                           cv2.resize(cs, (sw, sh), interpolation=cv2.INTER_CUBIC),
                           out=C[y0:y1, x0:x1])
                boxes.append((x0, y0, x1, y1))
                info['windows'] += 1
            if partial:
                break
        if partial:
            break

    mask = ((S > 0) & (C <= 0)).astype(np.uint8)

    if EXTEND_BY_COLOUR:
        mask = _extend_by_colour(img, mask)

    if SMOOTH_PX > 0:
        k = int(2 * SMOOTH_PX + 1)
        smoothed = (cv2.blur(mask.astype(np.float32), (k, k)) > 0.45).astype(np.uint8)
        # clothing still wins after rounding, except where colour proved bare skin
        smoothed[(C > 0) & (mask == 0)] = 0
        mask = smoothed

    _paint(out, img, mask, cover)
    info['painted'] = int(mask.any())
    info['partial'] = partial
    info['wash'] = not bool(mask.any())
    info['seconds'] = round(time.time() - t0, 1)
    return out, info


if __name__ == '__main__':
    import sys
    im = cv2.imread(sys.argv[1])
    o, i = process(im)
    print(i)
    cv2.imwrite(sys.argv[2], o)
