"""
Splitting a month into intermediate-file periods.

Extraction holds one period of one region in memory at a time (see
:func:`extract_um_met.extract.extract_region`), so the period length sets the peak
memory of the whole pipeline. A domain chooses it with ``intermediate_period:``
in config — ``month`` (the default, and the pipeline's original behaviour),
``week``, or ``day``.

Periods never cross a month boundary: a "week" is a 7-day block within the month
(1-7, 8-14, 15-21, 22-28, 29-end, so the last block is short). That keeps one Mk
version per period, and keeps the yearly store and its resume state keyed the way
they already are.
"""

import calendar


# Days per chunk; None means "the whole month, however long it is".
PERIOD_DAYS = {"month": None, "week": 7, "day": 1}


def resolve_intermediate_period(domain_cfg):
    """
    The domain's ``intermediate_period``, validated. Defaults to ``month``.

    An unknown period raises here, at config resolution, rather than part-way
    through a run.
    """
    period = domain_cfg.get("intermediate_period", "month")
    if period not in PERIOD_DAYS:
        raise ValueError(
            f"domain intermediate_period: unknown period {period!r}. "
            f"Valid periods: {list(PERIOD_DAYS)}"
        )
    return period


def month_days(year, month):
    """Every ``YYYYMMDD`` key in a month, in order."""
    n_days = calendar.monthrange(year, month)[1]
    return [f"{year}{month:02d}{d:02d}" for d in range(1, n_days + 1)]


def period_chunks(year, month, period):
    """
    Split a month into chunks of ``YYYYMMDD`` day keys, in time order.

    Each chunk is one extract → join → append pass: its regions are extracted,
    stitched, and appended to the yearly store before the next chunk starts.
    """
    days = month_days(year, month)
    step = PERIOD_DAYS[period]
    if step is None:
        return [days]
    return [days[i:i + step] for i in range(0, len(days), step)]


def period_tag(year, month, days):
    """
    The filename tag for a chunk of days.

    A chunk spanning the whole month is tagged ``YYYYMM`` — the name the pipeline
    has always given monthly intermediates, so existing scratch stores stay
    reusable — and any shorter chunk is tagged with its first day, ``YYYYMMDD``.
    """
    if len(days) == calendar.monthrange(year, month)[1]:
        return f"{year}{month:02d}"
    return days[0]
