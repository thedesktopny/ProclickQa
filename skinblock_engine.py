"""
Skin Block detection engine — runs on the VoiceGuard server.

Paints skin (and faces) with the cover colour. Grid screenshots are split into
their thumbnail tiles; each tile is processed as its own photo with paint
clipped to that tile. Includes: adaptive per-photo skin colour, smoothness
test (skin is smooth, bricks/wood grain are not), edge barriers (growth cannot
cross outlines, separating hands from same-coloured desks), pale-skin second
pass, macro-closeup rule, and a face-coverage guarantee. Tuned against real
agent screenshots.
"""
import numpy as np
import cv2

FRONTAL = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_alt2.xml')
FRONTAL2 = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
PROFILE = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_profileface.xml')
UPPER = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_upperbody.xml')

FIXED = dict(crMin=133, crMax=178, cbMin=77, cbMax=127)


def _ycrcb(img):
    ycc = cv2.cvtColor(img, cv2.COLOR_BGR2YCrCb).astype(np.int16)
    return ycc[:, :, 0], ycc[:, :, 1], ycc[:, :, 2]


def detect_faces(img):
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    gray = cv2.equalizeHist(gray)
    h, w = gray.shape
    mn = max(12, int(min(h, w) * 0.05))
    mx = int(min(h, w) * 0.55)          # a face bigger than half the photo is a false hit
    boxes = []
    for casc, neigh in ((FRONTAL, 3), (FRONTAL2, 4)):
        det = casc.detectMultiScale(gray, 1.05, neigh, minSize=(mn, mn), maxSize=(mx, mx))
        boxes += [tuple(b) for b in det]
    det = PROFILE.detectMultiScale(gray, 1.06, 4, minSize=(mn, mn), maxSize=(mx, mx))
    boxes += [tuple(b) for b in det]
    det = PROFILE.detectMultiScale(cv2.flip(gray, 1), 1.06, 4, minSize=(mn, mn), maxSize=(mx, mx))
    boxes += [(w - x - bw, y, bw, bh) for (x, y, bw, bh) in det]
    # sanity: a real face contains skin-coloured pixels; kills dark-panel ghosts
    y_, cr, cb = _ycrcb(img)
    skinish = ((cr >= FIXED['crMin']-10) & (cr <= FIXED['crMax']+10) &
               (cb >= FIXED['cbMin']-10) & (cb <= FIXED['cbMax']+10) & (y_ > 25))
    boxes = [b for b in boxes
             if skinish[int(b[1]+b[3]*0.2):int(b[1]+b[3]*0.85),
                        int(b[0]+b[2]*0.2):int(b[0]+b[2]*0.85)].mean() > 0.12]
    # dedupe by IoU
    boxes.sort(key=lambda b: -b[2] * b[3])
    kept = []
    for b in boxes:
        ok = True
        for k in kept:
            ix = max(0, min(b[0]+b[2], k[0]+k[2]) - max(b[0], k[0]))
            iy = max(0, min(b[1]+b[3], k[1]+k[3]) - max(b[1], k[1]))
            inter = ix * iy
            union = b[2]*b[3] + k[2]*k[3] - inter
            if union and inter/union > 0.3:
                ok = False
                break
        if ok:
            kept.append(b)
    return kept


def color_model(img, seed):
    y, cr, cb = _ycrcb(img)
    base = ((cr >= FIXED['crMin']-6) & (cr <= FIXED['crMax']+6) &
            (cb >= FIXED['cbMin']-6) & (cb <= FIXED['cbMax']+6) & (y > 25) & (y < 250))
    samp = base & (seed > 0)
    if samp.sum() < 100:
        return dict(FIXED)
    mcr, scr = float(cr[samp].mean()), max(3.0, float(cr[samp].std()))
    mcb, scb = float(cb[samp].mean()), max(3.0, float(cb[samp].std()))
    return dict(
        crMin=max(FIXED['crMin']-8, mcr - max(8, 2.8*scr)),
        crMax=min(FIXED['crMax']+8, mcr + max(8, 2.8*scr)),
        cbMin=max(FIXED['cbMin']-8, mcb - max(8, 2.8*scb)),
        cbMax=min(FIXED['cbMax']+8, mcb + max(8, 2.8*scb)))


def strict_mask(img, m):
    y, cr, cb = _ycrcb(img)
    b = img[:, :, 0].astype(np.int16); g = img[:, :, 1].astype(np.int16); r = img[:, :, 2].astype(np.int16)
    mx = np.maximum(np.maximum(r, g), b); mn = np.minimum(np.minimum(r, g), b)
    return ((r > 45) & (r - g >= 12) & (r > b) & (mx - mn >= 15) & (y > 30) & (y < 252) &
            (cr >= m['crMin']) & (cr <= m['crMax']) & (cb >= m['cbMin']) & (cb <= m['cbMax']))


def smooth_map(img):
    """Low local gradient = smooth surface (skin). Bricks/wood/fabric texture fail this."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    mag = cv2.magnitude(gx, gy)
    local = cv2.boxFilter(mag, -1, (7, 7))
    return local < 85


def grow(seed, allowed, iters=60):
    """Flood the seed outward through the allowed mask (reconstruction by dilation)."""
    seed = (seed & allowed).astype(np.uint8)
    kernel = np.ones((3, 3), np.uint8)
    prev = 0
    for _ in range(iters):
        seed = cv2.dilate(seed, kernel) & allowed
        s = int(seed.sum())
        if s == prev:
            break
        prev = s
    return seed


def skin_photo(img, faces=None, allow_macro=False):
    """Skin mask for one coherent photo/thumbnail."""
    h, w = img.shape[:2]
    if faces is None:
        faces = detect_faces(img)

    seed = np.zeros((h, w), np.uint8)
    for (x, y, bw, bh) in faces:
        seed[int(y+bh*0.25):int(y+bh*0.8), int(x+bw*0.25):int(x+bw*0.78)] = 1
    m = color_model(img, seed) if seed.sum() >= 100 else dict(FIXED)

    strict = strict_mask(img, m).astype(np.uint8)
    strict = cv2.morphologyEx(strict, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))

    # texture test: skin is smooth; bricks, wood grain, sand and fabric are not.
    smooth = smooth_map(img)
    y_, cr, cb = _ycrcb(img)
    # neutral guard: real skin always has chroma — white/grey backgrounds and
    # page furniture sit at cr~128/cb~128 and must never become growth territory
    b_ = img[:, :, 0].astype(np.int16); g_ = img[:, :, 1].astype(np.int16); r_ = img[:, :, 2].astype(np.int16)
    sat = np.maximum(np.maximum(r_, g_), b_) - np.minimum(np.minimum(r_, g_), b_)
    chroma = ((cr >= max(131, m['crMin']-3)) & (cr <= m['crMax']+3) &
              (cb >= m['cbMin']-3) & (cb <= min(126, m['cbMax']+3)) &
              (y_ > 22) & (y_ < 252) & (sat >= 9))

    # seeds = strict hits that are smooth; then flood each seed outward through
    # smooth, skin-chroma territory — one solid blob per skin area, shadows filled
    # macro-skin rule: when a big share of the photo is confidently skin, this
    # is a face/body closeup — the flood caps (built to stop background washes)
    # would wrongly strangle it. Paint the full skin field directly.
    if (allow_macro or faces) and float(strict.mean()) > 0.12:
        y2_, cr2, cb2 = y_, cr, cb
        wide = ((cr2 >= max(130, m['crMin']-5)) & (cr2 <= m['crMax']+5) &
                (cb2 >= m['cbMin']-6) & (cb2 <= 127) & (y2_ > 25) & (y2_ < 253) & (sat >= 4))
        sk = ((wide & smooth) | (strict > 0)).astype(np.uint8)
        sk = cv2.morphologyEx(sk, cv2.MORPH_CLOSE, np.ones((13, 13), np.uint8))
        n3, lab3, st3, _ = cv2.connectedComponentsWithStats(sk)
        keep3 = np.zeros(n3, bool)
        for i in range(1, n3):
            if st3[i, cv2.CC_STAT_AREA] >= sk.size * 0.004:
                keep3[i] = True
        sk = keep3[lab3].astype(np.uint8)
        sk = cv2.dilate(sk, np.ones((7, 7), np.uint8))
        for (x, y, bw, bh) in faces:
            cv2.ellipse(sk, (int(x + bw/2), int(y + bh/2)),
                        (int(bw*0.72), int(bh*0.92)), 0, 0, 360, 1, -1)
        return sk, faces

    # edge barriers: growth may not cross strong image edges. This is what
    # separates a hand from a same-coloured desk — the hand's outline is an
    # edge, so hand and desk become different components and only the desk
    # gets dropped as background.
    gray_e = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    barrier = cv2.dilate(cv2.Canny(gray_e, 40, 110), np.ones((3, 3), np.uint8)) > 0

    core = cv2.erode(strict, np.ones((5, 5), np.uint8))
    seed_ok = ((((strict > 0) & smooth) | (core > 0)) & ~barrier).astype(np.uint8)
    sk = grow(seed_ok, (((chroma & smooth) | (strict > 0)) & ~barrier).astype(np.uint8))
    # flood cap: keep a grown component only if a fair share of it was real seed
    n0, lab0 = cv2.connectedComponents(sk)
    if n0 > 1:
        seed_counts = np.bincount(lab0[seed_ok > 0], minlength=n0)
        comp_counts = np.bincount(lab0.ravel(), minlength=n0)
        ok = seed_counts * 14 >= comp_counts
        ok[0] = False
        sk = ok[lab0].astype(np.uint8)
    # stage 2: pale/blown-out skin (highlight cheeks, very fair tones) sits at
    # tiny saturation where stage 1 cannot go. Grow a second, tighter ring out
    # of confirmed skin through bright smooth territory with any warm lean.
    pale_ok = (smooth & (y_ > 90) & (y_ < 253) & (sat >= 4) &
               (cr >= 130) & (cr <= m['crMax']+4) & (cb >= m['cbMin']-6) & (cb <= 127))
    before = sk.copy()
    sk2 = grow(sk, (pale_ok | (sk > 0)).astype(np.uint8), iters=30)
    n2, lab2 = cv2.connectedComponents(sk2)
    if n2 > 1:
        base_counts = np.bincount(lab2[before > 0], minlength=n2)
        comp_counts = np.bincount(lab2.ravel(), minlength=n2)
        ok2 = base_counts * 6 >= comp_counts
        ok2[0] = False
        sk = np.where(ok2[lab2], sk2, before).astype(np.uint8)
    sk = cv2.morphologyEx(sk, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))

    # component filter: drop specks; drop background washes unless face-anchored
    n, lab, stats, _ = cv2.connectedComponentsWithStats(sk)
    keep = np.zeros(n, bool)
    total = h * w
    anchor = np.zeros((h, w), np.uint8)
    for (x, y, bw, bh) in faces:
        x0 = int(max(0, x - bw*0.7)); x1 = int(min(w, x + bw*1.7))
        y0 = int(max(0, y - bh*0.7)); y1 = int(min(h, y + bh*3.2))
        anchor[y0:y1, x0:x1] = 1
    for i in range(1, n):
        area = stats[i, cv2.CC_STAT_AREA]
        if area < total * 0.0012:
            continue
        comp_anchor = anchor[lab == i].any() if faces else False
        if area > total * 0.6 and not comp_anchor:
            skin_photo.last_wash = True
            continue  # background wash
        keep[i] = True
    sk = keep[lab].astype(np.uint8)

    sk = cv2.dilate(sk, np.ones((7, 7), np.uint8))

    # face guarantee — every face fully covered even if colour failed on it
    for (x, y, bw, bh) in faces:
        cx, cy = int(x + bw/2), int(y + bh/2)
        cv2.ellipse(sk, (cx, cy), (int(bw*0.72), int(bh*0.92)), 0, 0, 360, 1, -1)
    return sk, faces


def find_tiles(img):
    h, w = img.shape[:2]
    scale = min(1.0, 1400.0 / max(h, w))
    small = cv2.resize(img, (int(w*scale), int(h*scale))) if scale < 1 else img
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.int16)
    dx = np.abs(np.diff(gray, axis=1, prepend=gray[:, :1]))
    dy = np.abs(np.diff(gray, axis=0, prepend=gray[:1, :]))
    detail = ((dx > 9) | (dy > 9)).astype(np.uint8)
    detail = cv2.morphologyEx(detail, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    detail = cv2.morphologyEx(detail, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    n, lab, stats, _ = cv2.connectedComponentsWithStats(detail)

    def split_rect(x, y, bw, bh, depth=0):
        """Recursively split a region at flat rows/columns (masonry gutters)."""
        LIMIT = 380
        if depth >= 4 or (bw <= LIMIT and bh <= LIMIT):
            return [(x, y, bw, bh)]
        sub = detail[y:y+bh, x:x+bw]
        rows = sub.sum(axis=1) / max(1, bw)
        cols = sub.sum(axis=0) / max(1, bh)
        # find the emptiest interior valley (>=15% in from each side)
        def best_cut(profile, length):
            lo, hi = int(length*0.15), int(length*0.85)
            if hi - lo < 8:
                return None, 1.0
            i = int(np.argmin(profile[lo:hi])) + lo
            return i, float(profile[i])
        rcut, rval = best_cut(rows, bh)
        ccut, cval = best_cut(cols, bw)
        # cut along the flatter valley if it is genuinely flat-ish
        if min(rval, cval) > 0.35:
            return [(x, y, bw, bh)]
        if rval <= cval:
            return split_rect(x, y, bw, rcut, depth+1) + split_rect(x, y+rcut, bw, bh-rcut, depth+1)
        return split_rect(x, y, ccut, bh, depth+1) + split_rect(x+ccut, y, bw-ccut, bh, depth+1)

    tiles = []
    for i in range(1, n):
        x, y, bw, bh, area = stats[i]
        if bw < 16 or bh < 16 or area < 300:
            continue
        for (px, py, pw, ph) in split_rect(x, y, bw, bh):
            # trim empty margins of each piece
            piece = detail[py:py+ph, px:px+pw]
            ys, xs = np.where(piece > 0)
            if ys.size < 90:
                continue
            qx, qy = px + xs.min(), py + ys.min()
            qw, qh = xs.max() - xs.min() + 1, ys.max() - ys.min() + 1
            if qw < 13 or qh < 13:
                continue
            pad = 2
            fx = max(0, int(qx/scale) - pad); fy = max(0, int(qy/scale) - pad)
            fw = min(w - fx, int(qw/scale) + pad*2); fh = min(h - fy, int(qh/scale) + pad*2)
            if fw >= 18 and fh >= 18:
                tiles.append((fx, fy, fw, fh))
    return tiles


def process(img, cover=(0, 0, 0), grid_face_threshold=4, debug=None):
    h, w = img.shape[:2]
    out = img.copy()
    tiles = find_tiles(img)
    # decide grid by counting faces inside tiles at working scale
    grid = len(tiles) >= 5
    info = dict(grid=grid, tiles=len(tiles), painted=0, skipped=0)

    if not grid:
        skin_photo.last_wash = False
        sk, faces = skin_photo(img)  # single photo: macro only with a face
        out[sk > 0] = cover
        info['faces'] = len(faces)
        info['wash'] = bool(getattr(skin_photo, 'last_wash', False)) and sk.sum() == 0
        return out, info

    for (tx, ty, tw, th) in tiles:
        crop = img[ty:ty+th, tx:tx+tw]
        up = min(4.0, max(1.0, 460.0 / max(tw, th)))
        work = cv2.resize(crop, (int(tw*up), int(th*up)), interpolation=cv2.INTER_LINEAR) if up > 1 else crop.copy()

        faces = detect_faces(work)
        if not faces:
            big = cv2.resize(work, None, fx=1.6, fy=1.6, interpolation=cv2.INTER_LINEAR)
            faces = [(x/1.6, y/1.6, bw/1.6, bh/1.6) for (x, y, bw, bh) in detect_faces(big)]
        if not faces:
            q = (strict_mask(work, FIXED) & smooth_map(work)).astype(np.uint8)
            q = cv2.morphologyEx(q, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
            nn, _, st, _ = cv2.connectedComponentsWithStats(q)
            biggest = st[1:, cv2.CC_STAT_AREA].max() if nn > 1 else 0
            if biggest < q.size * 0.002 and biggest < 700:
                info['skipped'] += 1
                continue

        sk, faces = skin_photo(work, faces, allow_macro=True)
        if not sk.any():
            info['skipped'] += 1
            continue
        skf = cv2.resize(sk, (tw, th), interpolation=cv2.INTER_NEAREST)
        region = out[ty:ty+th, tx:tx+tw]
        region[skf > 0] = cover
        info['painted'] += 1
        if debug is not None:
            cv2.rectangle(out, (tx, ty), (tx+tw, ty+th), (0, 200, 0), 1)

    return out, info
