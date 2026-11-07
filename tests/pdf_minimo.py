"""PDF de una página con una capa de texto dada, sin dependencias (para los tests de lectura)."""

from __future__ import annotations


def _esc(t: str) -> str:
    return t.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def pdf_texto(texto: str) -> bytes:
    """PDF mínimo (Helvetica, WinAnsi) con una línea de texto por línea de `texto`."""
    lineas = [ln.encode("cp1252", "replace").decode("latin-1") for ln in texto.splitlines()]
    partes = [f"({_esc(ln[:110])}) Tj T*" for ln in lineas]
    cont = "BT /F1 9 Tf 12 TL 30 800 Td\n" + "\n".join(partes) + "\nET"
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 842] /Contents 4 0 R "
        "/Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(cont)} >>\nstream\n{cont}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    ]
    out = "%PDF-1.4\n"
    offs = []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n"
    out += "".join(f"{o:010d} 00000 n \n" for o in offs)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    return out.encode("latin-1")
