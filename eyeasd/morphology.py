import numpy as np


def _flood(free: bytearray, seeds, width: int) -> list[int]:
    out, stack = [], []
    for s in seeds:
        if free[s]:
            free[s] = 0
            stack.append(s)
    while stack:
        i = stack.pop()
        out.append(i)
        for j in (i - 1, i + 1, i - width, i + width):
            if free[j]:
                free[j] = 0
                stack.append(j)
    return out


def fill_holes(mask: np.ndarray) -> np.ndarray:
    if not mask.any():
        return mask.copy()
    ys, xs = np.nonzero(mask)
    y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    sub = np.pad(mask[y0:y1, x0:x1], 1, constant_values=False)
    h, w = sub.shape
    free = bytearray((~sub).astype(np.uint8).tobytes())
    ring = [i for i in range(w)] + [(h - 1) * w + i for i in range(w)] + [r * w for r in range(h)] + [r * w + w - 1 for r in range(h)]
    guarded = bytearray(len(free) + 2 * w + 2)
    guarded[w + 1 : w + 1 + len(free)] = free
    outside = np.zeros(len(guarded), bool)
    outside[_flood(guarded, [i + w + 1 for i in ring], w)] = True
    reached = outside[w + 1 : w + 1 + h * w].reshape(h, w)[1:-1, 1:-1]
    out = mask.copy()
    out[y0:y1, x0:x1] = ~reached
    return out


def largest_component(mask: np.ndarray) -> np.ndarray:
    if not mask.any():
        return mask.copy()
    h, w = mask.shape
    padded = np.pad(mask, 1, constant_values=False)
    free = bytearray(padded.astype(np.uint8).tobytes())
    best: list[int] = []
    for start in np.flatnonzero(padded).tolist():
        if free[start]:
            comp = _flood(free, [start], w + 2)
            if len(comp) > len(best):
                best = comp
    out = np.zeros(padded.size, bool)
    out[best] = True
    return fill_holes(out.reshape(h + 2, w + 2)[1:-1, 1:-1])


def erode(mask: np.ndarray, iterations: int = 1) -> np.ndarray:
    m = mask
    for _ in range(iterations):
        p = np.pad(m, 1, constant_values=False)
        m = p[1:-1, 1:-1] & p[:-2, 1:-1] & p[2:, 1:-1] & p[1:-1, :-2] & p[1:-1, 2:]
    return m


def box_mean(a: np.ndarray, size: int) -> np.ndarray:
    out = a.astype(np.float64)
    before, after = size // 2, size - 1 - size // 2
    for axis in (0, 1):
        pad = [(0, 0), (0, 0)]
        pad[axis] = (before, after)
        c = np.cumsum(np.pad(out, pad, mode="symmetric"), axis=axis)
        c = np.concatenate([np.zeros_like(np.take(c, [0], axis=axis)), c], axis=axis)
        n = out.shape[axis]
        out = (np.take(c, range(size, size + n), axis=axis) - np.take(c, range(n), axis=axis)) / size
    return out
