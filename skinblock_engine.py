"""
Skin Block detection engine - runs on the VoiceGuard server.

Primary detector: SegFormer human-parsing (segformer_b2_clothes, ONNX). It
labels every pixel as one of 18 classes - Face, Left-arm, Right-arm, Left-leg,
Right-leg, Hair, Upper-clothes, Pants, Dress, Skirt, Shoes, Bag, Hat, Scarf,
Sunglasses, Belt, Background - so skin and clothing are separated by a trained
model instead of colour rules. Only the skin classes are painted; every
clothing class stays visible.

The model downloads once on first use (the Azure server is not behind the
content filter) and is cached on disk. If it cannot be obtained, the engine
falls back to the older colour/texture detector so the tool still works, and
reports which detector ran so the page can warn the agent.

Grid screenshots are split into thumbnail tiles; each tile is parsed as its own
photo with paint clipped to that tile.
"""
import os
import urllib.request

import numpy as np
import cv2

MODEL_DIR = os.path.join(os.getenv('HOME', '.'), 'sbmodel')
MODEL_PATH = os.path.join(MODEL_DIR, 'segformer_b2_clothes.onnx')
# Quantized first: several times faster on a CPU App Service plan, which is what
# keeps a whole screenshot inside Azure's 230s request limit. Full precision is
# the fallback if the quantized export can't be fetched.
MODEL_URLS = [
    'https://huggingface.co/Xenova/segformer_b2_clothes/resolve/main/onnx/model_quantized.onnx',
    'https://huggingface.co/Xenova/segformer_b2_clothes/resolve/main/onnx/model.onnx',
]

# class ids in this model
BACKGROUND, HAT, HAIR, SUNGLASSES, UPPER, SKIRT, PANTS, DRESS, BELT = 0, 1, 2, 3, 4, 5, 6, 7, 8
LEFT_SHOE, RIGHT_SHOE, FACE, LEFT_LEG, RIGHT_LEG, LEFT_ARM, RIGHT_ARM, BAG, SCARF = 9, 10, 11, 12, 13, 14, 15, 16, 17
SKIN_CLASSES = (FACE, LEFT_LEG, RIGHT_LEG, LEFT_ARM, RIGHT_ARM)
# The reference look covers the whole head, hair included, as one smooth shape.
HEAD_CLASSES = (HAIR, HAT)
# Anything the model positively identified as worn. Rounding the mask may never
# cover these — that is what painted over the bikini.
CLOTH_CLASSES = (UPPER, SKIRT, PANTS, DRESS, BELT, LEFT_SHOE, RIGHT_SHOE, BAG, SUNGLASSES)
COVER_HAIR = False

# Shapes are smoothed into simple rounded blobs rather than traced pixel-exactly.
# 0 disables. Higher = rounder/simpler.
SMOOTH = 0.6

# 'solid' = one colour everywhere (cover_color).
# 'blend' = each covered area is filled with its own averaged tone, so the patch
#           sits in the picture the way the reference examples do.
COVER_MODE = 'blend'

# How many pixels to grow the mask past the model's own edge. 0 = follow the
# model exactly (cleanest, never touches clothing). Raise to 1-2 only if a thin
# rim of skin ever survives at the boundary.
SKIN_PAD = 0

_session = None
_session_tried = False
_model_error = ''

INPUT_SIZE = 512
MEAN = np.array([0.485, 0.456, 0.406], np.float32)
STD = np.array([0.229, 0.224, 0.225], np.float32)


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
            if os.path.getsize(tmp) < 1024 * 1024:
                os.remove(tmp)
                last = 'downloaded file too small'
                continue
            os.replace(tmp, MODEL_PATH)
            return True
        except Exception as e:
            last = str(e)[:200]
    raise RuntimeError(last or 'download failed')


def get_session():
    """Loads the parser once per process. Returns None if unavailable."""
    global _session, _session_tried, _model_error
    if _session is not None or _session_tried:
        return _session
    _session_tried = True
    try:
        import onnxruntime as ort
        if not os.path.exists(MODEL_PATH) or os.path.getsize(MODEL_PATH) < 1024 * 1024:
            _download_model()
        so = ort.SessionOptions()
        so.intra_op_num_threads = max(1, (os.cpu_count() or 2))
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        _session = ort.InferenceSession(MODEL_PATH, so, providers=['CPUExecutionProvider'])
        global INPUT_SIZE
        try:
            shp = _session.get_inputs()[0].shape          # e.g. [1, 3, 512, 512] or ['b',3,'h','w']
            dynamic = not isinstance(shp[2], int) or not isinstance(shp[3], int)
            if dynamic:
                INPUT_SIZE = 512      # a bigger input is sharper but much slower
            elif isinstance(shp[2], int) and shp[2] > 0:
                INPUT_SIZE = int(shp[2])
        except Exception:
            pass
        print('[skinblock] parser model loaded, input size %d' % INPUT_SIZE)
    except Exception as e:
        _model_error = str(e)[:200]
        print('[skinblock] parser unavailable, using fallback: ' + _model_error)
        _session = None
    return _session


def model_status():
    return {'model': bool(get_session()), 'error': _model_error}


def _auto_levels(img_bgr):
    """Brightens a dark crop before it goes to the model. Dark thumbnails read
    as background to a model trained on normally-lit photos. Input only — the
    picture the agent receives is untouched."""
    mean = float(cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY).mean())
    if mean <= 4 or mean >= 105:
        return img_bgr
    gamma = max(0.35, min(1.0, np.log(0.42) / np.log(mean / 255.0)))
    lut = np.array([round(255 * (i / 255.0) ** gamma) for i in range(256)], np.uint8)
    return cv2.LUT(img_bgr, lut)


def _preprocess(img_bgr):
    img_bgr = _auto_levels(img_bgr)
    rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
    size = max(256, min(1024, int(INPUT_SIZE) // 32 * 32))
    rgb = cv2.resize(rgb, (size, size), interpolation=cv2.INTER_LINEAR)
    x = rgb.astype(np.float32) / 255.0
    x = (x - MEAN) / STD
    return np.transpose(x, (2, 0, 1))[None, ...].astype(np.float32)


def parse_labels(img_bgr):
    """Per-pixel class-id map at the image's own size, or None if no model."""
    sess = get_session()
    if sess is None:
        return None
    try:
        inp = sess.get_inputs()[0].name
        out = sess.run(None, {inp: _preprocess(img_bgr)})[0]
        logits = out[0] if out.ndim == 4 else out          # (C, h, w)
        h, w = img_bgr.shape[:2]
        # smooth (bilinear) upsample of every class score, then pick the winner
        # per full-resolution pixel — this is what gives clean curved edges
        # instead of the blocky staircase of a nearest-neighbour label map.
        chans = [cv2.resize(c, (w, h), interpolation=cv2.INTER_LINEAR) for c in logits]
        big = np.stack(chans, axis=0)
        return np.argmax(big, axis=0).astype(np.uint8)
    except Exception as e:
        print('[skinblock] inference failed: ' + str(e)[:160])
        return None


def _smooth_mask(sk):
    """Rounds the outline into a simple blob — blur the mask and re-threshold.
    This is what turns a jagged traced edge into the soft shapes in the
    reference images, without letting the shape shrink away from the skin."""
    if SMOOTH <= 0 or not sk.any():
        return sk
    h, w = sk.shape[:2]
    k = int(round(min(h, w) * 0.02 * float(SMOOTH)))
    k = max(3, k | 1)
    blur = cv2.GaussianBlur(sk.astype(np.float32) * 255.0, (k, k), 0)
    # threshold below the midpoint so smoothing never pulls the edge inside the
    # real skin — it rounds corners outward instead of eroding coverage
    out = (blur > 100).astype(np.uint8)
    return cv2.morphologyEx(out, cv2.MORPH_CLOSE, np.ones((k, k), np.uint8))


def skin_from_labels(labels, include_neck=False):
    if labels is None:
        return None
    wanted = list(SKIN_CLASSES) + ([SCARF] if include_neck else [])
    if COVER_HAIR:
        wanted += list(HEAD_CLASSES)
    sk = np.isin(labels, wanted).astype(np.uint8)
    if not sk.any():
        return sk
    # remember what is clothing so the smoothing below can't creep onto it
    cloth = np.isin(labels, CLOTH_CLASSES)
    # close pinholes only (glasses, jewellery, a stray highlight). No dilation:
    # growing the mask is what pushed the paint onto collars and straps, and the
    # model's own boundary is already the true skin/clothing line.
    sk = cv2.morphologyEx(sk, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    sk = _smooth_mask(sk)
    pad = int(SKIN_PAD)
    if pad > 0:
        sk = cv2.dilate(sk, np.ones((pad * 2 + 1, pad * 2 + 1), np.uint8))
    # hard rule, applied last: never paint over something the model called
    # clothing, no matter how the rounding or padding expanded the shape
    sk[cloth] = 0
    return sk


# ---------- fallback detector (only if the model can't be loaded) ----------

FIXED = dict(crMin=133, crMax=178, cbMin=77, cbMax=127)
FRONTAL = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_alt2.xml')


def _ycrcb(img):
    ycc = cv2.cvtColor(img, cv2.COLOR_BGR2YCrCb).astype(np.int16)
    return ycc[:, :, 0], ycc[:, :, 1], ycc[:, :, 2]


def strict_mask(img, m=FIXED):
    y, cr, cb = _ycrcb(img)
    b = img[:, :, 0].astype(np.int16)
    g = img[:, :, 1].astype(np.int16)
    r = img[:, :, 2].astype(np.int16)
    mx = np.maximum(np.maximum(r, g), b)
    mn = np.minimum(np.minimum(r, g), b)
    return ((r > 45) & (r - g >= 12) & (r > b) & (mx - mn >= 15) & (y > 30) & (y < 252) &
            (cr >= m['crMin']) & (cr <= m['crMax']) & (cb >= m['cbMin']) & (cb <= m['cbMax']))


def smooth_map(img):
    gray = cv2.GaussianBlur(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), (3, 3), 0)
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    return cv2.boxFilter(cv2.magnitude(gx, gy), -1, (7, 7)) < 85


def fallback_skin(img):
    h, w = img.shape[:2]
    sk = (strict_mask(img) & smooth_map(img)).astype(np.uint8)
    sk = cv2.morphologyEx(sk, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    sk = cv2.morphologyEx(sk, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    n, lab, stats, _ = cv2.connectedComponentsWithStats(sk)
    keep = np.zeros(n, bool)
    for i in range(1, n):
        a = stats[i, cv2.CC_STAT_AREA]
        if sk.size * 0.0015 <= a <= sk.size * 0.6:
            keep[i] = True
    sk = keep[lab].astype(np.uint8)
    gray = cv2.equalizeHist(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY))
    mn = max(12, int(min(h, w) * 0.05))
    for (x, y, bw, bh) in FRONTAL.detectMultiScale(gray, 1.06, 4, minSize=(mn, mn)):
        cv2.ellipse(sk, (int(x + bw / 2), int(y + bh / 2)),
                    (int(bw * 0.72), int(bh * 0.92)), 0, 0, 360, 1, -1)
    return cv2.dilate(sk, np.ones((5, 5), np.uint8))


# ---------- tile detection ----------

def find_tiles(img):
    h, w = img.shape[:2]
    scale = min(1.0, 1400.0 / max(h, w))
    small = cv2.resize(img, (int(w * scale), int(h * scale))) if scale < 1 else img
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.int16)
    dx = np.abs(np.diff(gray, axis=1, prepend=gray[:, :1]))
    dy = np.abs(np.diff(gray, axis=0, prepend=gray[:1, :]))
    detail = ((dx > 9) | (dy > 9)).astype(np.uint8)
    detail = cv2.morphologyEx(detail, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    detail = cv2.morphologyEx(detail, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    n, lab, stats, _ = cv2.connectedComponentsWithStats(detail)

    def best_cut(profile, length):
        lo, hi = int(length * 0.15), int(length * 0.85)
        if hi - lo < 8:
            return None, 1.0
        i = int(np.argmin(profile[lo:hi])) + lo
        return i, float(profile[i])

    def split_rect(x, y, bw, bh, depth=0):
        if depth >= 4 or (bw <= 380 and bh <= 380):
            return [(x, y, bw, bh)]
        sub = detail[y:y + bh, x:x + bw]
        rcut, rval = best_cut(sub.sum(axis=1) / max(1, bw), bh)
        ccut, cval = best_cut(sub.sum(axis=0) / max(1, bh), bw)
        if min(rval, cval) > 0.35:
            return [(x, y, bw, bh)]
        if rval <= cval:
            return split_rect(x, y, bw, rcut, depth + 1) + split_rect(x, y + rcut, bw, bh - rcut, depth + 1)
        return split_rect(x, y, ccut, bh, depth + 1) + split_rect(x + ccut, y, bw - ccut, bh, depth + 1)

    tiles = []
    for i in range(1, n):
        x, y, bw, bh, area = stats[i]
        if bw < 16 or bh < 16 or area < 300:
            continue
        for (px, py, pw, ph) in split_rect(x, y, bw, bh):
            piece = detail[py:py + ph, px:px + pw]
            ys, xs = np.where(piece > 0)
            if ys.size < 90:
                continue
            qx, qy = px + xs.min(), py + ys.min()
            qw, qh = xs.max() - xs.min() + 1, ys.max() - ys.min() + 1
            if qw < 13 or qh < 13:
                continue
            pad = 2
            fx = max(0, int(qx / scale) - pad)
            fy = max(0, int(qy / scale) - pad)
            fw = min(w - fx, int(qw / scale) + pad * 2)
            fh = min(h - fy, int(qh / scale) + pad * 2)
            if fw >= 18 and fh >= 18:
                tiles.append((fx, fy, fw, fh))
    return tiles


# ---------- main entry ----------

# Each model run costs real CPU time, so we do NOT run it once per thumbnail —
# that was 29 runs on one screenshot and blew past Azure's 230s gateway limit.
# Instead the image is covered by a small number of overlapping windows, each
# fed to the model at full input size. Roughly 6 runs instead of 29, with the
# same detail per thumbnail, because each window is ~700px of original image
# upscaled to the model's 512px input.
WINDOW_PX = 380          # how much original image each model run covers
WINDOW_OVERLAP = 0.12
MAX_WINDOWS = 15         # hard ceiling so one huge image can't run away
TIME_BUDGET = 110.0      # seconds of model time; Azure kills the request at ~230,
                         # and decode, smoothing and PNG encode need room too
_speed = None            # measured seconds per model run on this server


def _measure_speed(img):
    """One timed inference so we know what this server can actually afford.
    Cached per process — the answer doesn't change between requests."""
    global _speed
    if _speed is not None:
        return _speed
    import time as _time
    probe = cv2.resize(img, (INPUT_SIZE, INPUT_SIZE), interpolation=cv2.INTER_AREA)
    t = _time.time()
    parse_labels(probe)
    _speed = max(0.05, _time.time() - t)
    print('[skinblock] one model run takes %.1fs on this server' % _speed)
    return _speed


def model_skin(img, include_neck=False, budget=TIME_BUDGET):
    """Skin mask for a whole image via overlapping windows. None if no model."""
    import time as _time
    if get_session() is None:
        return None, False
    h, w = img.shape[:2]

    # How many runs fit in the budget on THIS server? Measure, don't assume —
    # a fixed window count is what caused the gateway timeouts.
    per_run = _measure_speed(img)
    affordable = max(1, int((budget - per_run) / per_run))

    cols = max(1, int(round(w / float(WINDOW_PX))))
    rows = max(1, int(round(h / float(WINDOW_PX))))
    limit = min(MAX_WINDOWS, affordable)
    while cols * rows > limit and (cols > 1 or rows > 1):
        if cols >= rows and cols > 1:
            cols -= 1
        elif rows > 1:
            rows -= 1
        else:
            break
    print('[skinblock] %dx%d windows (%d runs, ~%.0fs of %.0fs budget)'
          % (cols, rows, cols * rows, cols * rows * per_run, budget))

    mask = np.zeros((h, w), np.uint8)
    cw, ch = w / float(cols), h / float(rows)
    started = _time.time()
    partial = False
    for ry in range(rows):
        for rx in range(cols):
            if _time.time() - started > budget - per_run * 0.5:
                partial = True
                break
            x0 = int(max(0, rx * cw - cw * WINDOW_OVERLAP))
            y0 = int(max(0, ry * ch - ch * WINDOW_OVERLAP))
            x1 = int(min(w, (rx + 1) * cw + cw * WINDOW_OVERLAP))
            y1 = int(min(h, (ry + 1) * ch + ch * WINDOW_OVERLAP))
            if x1 - x0 < 16 or y1 - y0 < 16:
                continue
            sub = img[y0:y1, x0:x1]
            sk = skin_from_labels(parse_labels(sub), include_neck)
            if sk is None:
                continue
            np.maximum(mask[y0:y1, x0:x1], sk, out=mask[y0:y1, x0:x1])
        if partial:
            break
    return mask, partial


def _tile_skin(crop, use_model, include_neck=False):
    """Fallback-detector path, still per tile (it is cheap)."""
    th, tw = crop.shape[:2]
    up = min(4.0, max(1.0, 384.0 / max(tw, th)))
    work = cv2.resize(crop, (int(tw * up), int(th * up)), interpolation=cv2.INTER_LINEAR) if up > 1 else crop
    sk = fallback_skin(work)
    if sk.shape[:2] != (th, tw):
        sk = cv2.resize(sk, (tw, th), interpolation=cv2.INTER_NEAREST)
    return sk


def paint(out, img, mask, cover):
    """Applies the mask. In 'blend' mode each separate covered area is filled
    with its own averaged colour, so the patch matches the picture instead of
    being a black hole. In 'solid' mode everything gets the cover colour."""
    if not mask.any():
        return
    if COVER_MODE != 'blend':
        out[mask > 0] = cover
        return
    n, lab, stats, _ = cv2.connectedComponentsWithStats(mask)
    for i in range(1, n):
        sel = lab == i
        if stats[i, cv2.CC_STAT_AREA] < 12:
            out[sel] = cover
            continue
        mean = img[sel].reshape(-1, 3).mean(axis=0)
        # flatten and slightly mute it so the patch reads as a filled shape
        tone = np.clip(mean * 0.92 + 14, 0, 255).astype(np.uint8)
        out[sel] = tone


def process(img, cover=(0, 0, 0), grid_face_threshold=4, include_neck=False):
    """Paint skin. Returns (painted_image, info)."""
    import time as _time
    t0 = _time.time()
    out = img.copy()
    use_model = get_session() is not None
    info = dict(detector='model' if use_model else 'fallback',
                grid=False, tiles=0, painted=0, skipped=0, partial=False)

    if use_model:
        mask, partial = model_skin(img, include_neck)
        if mask is not None:
            paint(out, img, mask, cover)
            info['painted'] = int(mask.any())
            info['partial'] = partial
            info['windows'] = True
            info['seconds'] = round(_time.time() - t0, 1)
            return out, info
        use_model = False
        info['detector'] = 'fallback'

    # fallback detector: per tile, since it is cheap
    tiles = find_tiles(img)
    info['grid'] = len(tiles) >= 5
    info['tiles'] = len(tiles)
    if not info['grid']:
        sk = _tile_skin(img, False, include_neck)
        paint(out, img, sk, cover)
        info['painted'] = 1 if sk.any() else 0
        info['wash'] = not bool(sk.any())
        info['seconds'] = round(_time.time() - t0, 1)
        return out, info

    for (tx, ty, tw, th) in tiles:
        crop = img[ty:ty + th, tx:tx + tw]
        if crop.size == 0:
            continue
        sk = _tile_skin(crop, False, include_neck)
        if not sk.any():
            info['skipped'] += 1
            continue
        paint(out[ty:ty + th, tx:tx + tw], img[ty:ty + th, tx:tx + tw], sk, cover)
        info['painted'] += 1
    info['seconds'] = round(_time.time() - t0, 1)
    return out, info


if __name__ == '__main__':
    import sys, time
    im = cv2.imread(sys.argv[1])
    t = time.time()
    o, i = process(im)
    print(i, '%.1fs' % (time.time() - t))
    cv2.imwrite(sys.argv[2], o)
