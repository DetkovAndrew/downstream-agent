"""Рабочий процесс извлечения таблиц: изолирован, потому что PyMuPDF find_tables() иногда падает насмерть (сегфолт)."""


def table_worker(pdf: str, pages: list[int], q) -> None:
    import pymupdf
    doc = pymupdf.open(pdf)
    for n in pages:
        q.put(("start", n))
        try:
            rows = [t.extract() for t in doc[n - 1].find_tables().tables]
        except Exception:  # noqa: BLE001
            rows = []
        q.put(("done", n, rows))
    q.put(("end",))
