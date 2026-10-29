"""Yerel, sağlayıcıdan bağımsız stok ve satın alma hesapları."""

import csv
import json
import re
import unicodedata
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from lfx.custom import Component
from lfx.io import MessageTextInput, Output
from lfx.schema.message import Message

MAX_INPUT_CHARS, MAX_ROWS = 100_000, 100
NUMBERS = ("mevcutstok", "ayrilmisstok", "kesinaciksiparis", "gunluktuketim", "termingun", "emniyetstok")
REQUIRED = {"malzemekodu", "birim", *NUMBERS}
ASSUMPTIONS = [
    "Ayrılmış stok günlük tüketimdeki ihtiyaçtan ayrıdır; aynı talep iki kez düşülmemelidir.",
    "Kesin açık sipariş teslim alınmamış kalan miktardır ve mevcut stokta bulunmaz.",
    "Günlük tüketim sabittir; tarihlenmiş teslimler günün başında gerçekleşir.",
    "Hedef = günlük tüketim × termin + emniyet; öneri = max(0, hedef − net − dahil sipariş).",
    "Birimler ayrı tutulur. MOQ, paket katı, bütçe ve sonraki dönem talebi verilmemiştir.",
    "Bu basit kural termin bitimini karşılaştırır; acil satırda normal terminli yeni alım zamanında yetişmeyebilir.",
]


def _normalize(value):
    text = unicodedata.normalize("NFKD", value.casefold().replace("ı", "i"))
    return "".join(char for char in text if char.isascii() and char.isalnum())


def _number(raw, field, issues):
    raw = raw.strip()
    if not raw:
        issues.append(f"{field}: değer eksik")
        return None
    if len(raw) > 32 or not re.fullmatch(r"[+-]?\d+(?:[.,]\d{1,6})?", raw):
        issues.append(f"{field}: geçersiz sayı (en çok 6 ondalık; binlik ayırıcı yok)")
        return None
    try:
        value = Decimal(raw.replace(",", "."))
    except InvalidOperation:
        value = Decimal("NaN")
    if not value.is_finite() or value < 0 or value > Decimal("1e12"):
        issues.append(f"{field}: sayı negatif, sonlu değil veya 10^12 sınırını aşıyor")
        return None
    if field == "termingun" and (value != value.to_integral_value() or value > 3650):
        issues.append("termingun: 0–3650 aralığında tam gün olmalı")
        return None
    return value


def _iso_date(raw):
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _json_number(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else format(value.normalize(), "f")
    return value


def _analyze_row(source, report_date, row_number):
    issues, notes = [], []
    row = {
        "satir": row_number, "malzeme_kodu": source.get("malzemekodu", "").strip(),
        "tanim": source.get("tanim", "").strip(), "birim": source.get("birim", "").strip().upper(),
        "net_stok": None, "termin_tuketimi": None, "hedef_stok": None,
        "dahil_acik_siparis": None, "onerilen_miktar": None,
        "teslim_oncesi_stok": None, "termin_sonu_stok": None,
        "durum": "Bilgi eksik", "eksikler": issues, "notlar": notes,
    }
    values = {field: _number(source.get(field, ""), field, issues) for field in NUMBERS}
    if not row["malzeme_kodu"] or not row["birim"]:
        issues.append("malzeme_kodu veya birim eksik")
    if report_date is None:
        issues.append("Rapor tarihi eksik veya YYYY-MM-DD biçiminde geçerli değil")
    stock, reserved, order, daily, lead, safety = (values[field] for field in NUMBERS)
    if stock is not None and reserved is not None:
        row["net_stok"] = stock - reserved
        if row["net_stok"] < 0:
            row["durum"] = "Veri hatası"
            issues.append("Ayrılmış stok mevcut stoktan büyük")
    if daily is not None and lead is not None and safety is not None:
        row["termin_tuketimi"] = daily * lead
        row["hedef_stok"] = daily * lead + safety
    if issues:
        if any("değer eksik" not in issue
               and any(issue.startswith(field + ":") for field in NUMBERS) for issue in issues):
            row["durum"] = "Veri hatası"
        return {key: _json_number(value) for key, value in row.items()}
    try:
        horizon = report_date + timedelta(days=int(lead))
    except OverflowError:
        issues.append("Termin sonu tarihi takvim sınırını aşıyor")
        row["durum"] = "Veri hatası"
        return {key: _json_number(value) for key, value in row.items()}
    eligible, arrival_days = Decimal(0), None
    if order > 0:
        arrival = _iso_date(source.get("aciksiparisteslimtarihi", "").strip())
        if arrival is None or arrival < report_date:
            issues.append("Pozitif açık siparişin teslim tarihi eksik, geçersiz veya rapor tarihinden önce")
            return {key: _json_number(value) for key, value in row.items()}
        if arrival <= horizon:
            eligible, arrival_days = order, (arrival - report_date).days
        else:
            notes.append("Açık sipariş termin sonrasına kaldığı için hesaba dahil edilmedi")
    net = row["net_stok"]
    row["dahil_acik_siparis"] = eligible
    row["onerilen_miktar"] = max(Decimal(0), row["hedef_stok"] - net - eligible)
    row["termin_sonu_stok"] = net + eligible - daily * lead
    if arrival_days is not None:
        row["teslim_oncesi_stok"] = net - daily * arrival_days
    row["stok_gunu_siparis_haric"] = net / daily if daily else None
    if daily == 0:
        notes.append("Günlük tüketim sıfır; stok günü bölmesi yapılmadı")
    if row["teslim_oncesi_stok"] is not None and row["teslim_oncesi_stok"] < 0:
        row["durum"] = "Acil"
        notes.append("Açık sipariş gelmeden stok tükenir; teslimi öne çekmek veya ara tedarik gerekir")
    elif row["termin_sonu_stok"] < 0:
        row["durum"] = "Acil"
        notes.append("Termin bitmeden stok tükenir; normal terminli alım tek başına açığı gidermez")
    else:
        row["durum"] = "Satın alma gerekli" if row["termin_sonu_stok"] < safety else "Yeterli"
    return {key: _json_number(value) for key, value in row.items()}


def analyze_table(text: str) -> dict:
    """Noktalı virgüllü tek tabloyu hesaplar; modeller ve harici servisler kullanılmaz."""
    if len(text) > MAX_INPUT_CHARS:
        raise ValueError(f"Girdi en çok {MAX_INPUT_CHARS} karakter olabilir")
    result = {"report_date": None, "rows": [], "errors": [], "assumptions": ASSUMPTIONS.copy()}
    lines, header, header_index = text.splitlines(), [], None
    for index, line in enumerate(lines):
        if ";" in line:
            candidate = [_normalize(part) for part in next(csv.reader([line], delimiter=";"))]
            if len(REQUIRED.intersection(candidate)) >= 3:
                header, header_index = candidate, index
                break
    if header_index is None:
        result["errors"].append("Noktalı virgülle ayrılmış stok tablosu başlığı bulunamadı")
        return result
    if len(set(header)) != len(header):
        result["errors"].append("Tabloda yinelenen kolon adı var")
        return result
    if missing := sorted(REQUIRED - set(header)):
        result["errors"].append("Gerekli kolonlar eksik: " + ", ".join(missing))
        return result
    before = "\n".join(lines[:header_index]).casefold().replace("ı", "i")
    matches = re.findall(r"rapor\s+tarihi\s*:\s*(\d{4}-\d{2}-\d{2})(?!\d)", before)
    report_date = _iso_date(matches[0]) if len(set(matches)) == 1 else None
    result["report_date"] = report_date.isoformat() if report_date else None
    if not report_date:
        result["errors"].append("Tablodan önce tek, geçerli Rapor tarihi: YYYY-MM-DD gerekli")
    for index in range(header_index + 1, len(lines)):
        line = lines[index]
        if not line.strip():
            continue
        if ";" not in line:
            break
        if len(result["rows"]) >= MAX_ROWS:
            raise ValueError(f"Tablo en çok {MAX_ROWS} satır olabilir")
        try:
            cells = next(csv.reader([line], delimiter=";", strict=True))
        except csv.Error:
            cells = []
        if cells and len(cells) <= 2 and _normalize(cells[0]) in {
            "sonaciklama", "aciklama", "not", "notlar", "istek", "soru",
        }:
            continue
        source = dict(zip(header, cells, strict=False))
        row = _analyze_row(source, report_date, index + 1)
        if len(cells) != len(header):
            row["eksikler"].append("Satırdaki kolon sayısı başlıkla uyuşmuyor")
            row["onerilen_miktar"], row["durum"] = None, "Bilgi eksik"
        result["rows"].append(row)
    counts = Counter((row["malzeme_kodu"].casefold(), row["birim"].casefold()) for row in result["rows"])
    for row in result["rows"]:
        if counts[(row["malzeme_kodu"].casefold(), row["birim"].casefold())] > 1:
            row["onerilen_miktar"], row["durum"] = None, "Bilgi eksik"
            row["eksikler"].append("Kod + birim yineleniyor; çift sayımı önlemek için kesin öneri üretilmedi")
    if not result["rows"]:
        result["errors"].append("Tabloda veri satırı bulunamadı")
    return result


class StockProcurementRulesComponent(Component):
    display_name = "Stok ve Satın Alma Kuralları"
    description = "Tablodaki stok, termin ve sipariş miktarlarını kesin yerel kurallarla hesaplar."
    name = "StockProcurementRules"
    icon = "Calculator"
    inputs = [MessageTextInput(name="input_value", display_name="Özgün tablo", input_types=["Message"], required=True)]
    outputs = [Output(name="report", display_name="Tablo ve kesin hesaplar", method="build_report")]

    def build_report(self) -> Message:
        text = self.input_value if isinstance(self.input_value, str) else self.input_value.text
        report = analyze_table(text)
        self.status = f"{len(report['rows'])} satır hesaplandı"
        return Message(text="ÖZGÜN VERİ\n" + text + "\n\nKESİN YEREL HESAP (JSON)\n"
                       + json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
