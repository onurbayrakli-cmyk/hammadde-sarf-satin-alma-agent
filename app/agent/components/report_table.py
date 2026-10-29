"""Model metninden bağımsız, kaynak hesaplarıyla doğrulanan son tablo."""

import json
import ipaddress
from collections import Counter
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import quote, urlsplit, urlunsplit

from lfx.custom import Component
from lfx.io import MessageTextInput, Output
from lfx.schema.message import Message

MARKER = "KESİN YEREL HESAP (JSON)\n"
RESEARCH_MARKER = "SATIN ALMA ARAŞTIRMASI (JSON)\n"
PROPOSALS_MARKER = "STRATEJİK ÖNERİLER (JSON)\n"
FIELDS = ("malzeme_kodu", "birim", "net_stok", "hedef_stok", "dahil_acik_siparis", "onerilen_miktar", "durum")
TRIGGER_LABELS = {
    "fiyat_artisi_riski": "fiyat artışı riski",
    "arz_kesintisi_riski": "arz kesintisi riski",
    "teslim_suresi_riski": "teslim süresi riski",
    "stok_kritik": "kritik stok riski",
    "diger": "tedarik koşulları",
}


def _text(value):
    if value is None:
        return ""
    return value if isinstance(value, str) else value.text


def _cell(value):
    return "—" if value is None else str(value).replace("|", "\\|").replace("\n", " ")


def _bounded_text(value, limit=300):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        return None
    if any(ord(char) < 32 and char not in "\t\n\r" for char in value):
        return None
    return " ".join(value.split())


def _markdown(value):
    """Kaynak başlıklarını metin olarak gösterir; bağlantı/HTML ekletmez."""
    for char in "\\`*_{}[]()<>|~":
        value = value.replace(char, "\\" + char)
    return value


def _public_url(value):
    if not isinstance(value, str) or len(value) > 2000 or any(char.isspace() for char in value):
        return None
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        if parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
            return None
        if parsed.port is not None and parsed.port not in {80, 443}:
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            if "." not in host or host.lower().endswith((".local", ".localhost", ".internal", ".invalid", ".test")):
                return None
            labels = host.encode("idna").decode("ascii").split(".")
            if any(not label or len(label) > 63 or not all(char.isalnum() or char == "-" for char in label) for label in labels):
                return None
        # Parantez ve köşeli ayraçlar Markdown bağlantısını bozamaz.
        return urlunsplit((parsed.scheme, parsed.netloc, quote(parsed.path, safe="/%:@-._~"),
                           quote(parsed.query, safe="=&%:@/?-._~"), quote(parsed.fragment, safe="%-._~")))
    except (ValueError, UnicodeError):
        return None


def _parse_date(value):
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None


def _json_section(text, marker):
    if not isinstance(text, str) or marker not in text or len(text) > 250_000:
        return None
    try:
        payload = text.rsplit(marker, 1)[1].lstrip()
        if marker == PROPOSALS_MARKER:
            # Modelin yalnız ilk JSON nesnesi okunur; arkasındaki anlatım kullanılmaz.
            # Araştırma girdisi aynı toleransı almaz ve katı JSON olarak kalır.
            if payload.startswith("```"):
                fence, newline, payload = payload.partition("\n")
                if not newline or fence.strip().casefold() not in {"```", "```json"}:
                    return None
                payload = payload.lstrip()
            value, _ = json.JSONDecoder().raw_decode(payload)
        else:
            value = json.loads(payload)
        return value if isinstance(value, dict) else None
    except (ValueError, TypeError):
        return None


def _selection_codes(profile):
    """Stok dışı araştırma kalemi yalnız açık profil seçimiyle tanınır."""
    selected = profile.get("materials", []) if isinstance(profile, dict) else []
    if not isinstance(selected, list) or len(selected) > 100:
        return set()
    return {item["material_code"] for item in selected if isinstance(item, dict)
            and _bounded_text(item.get("material_code"), 120) and _bounded_text(item.get("name"), 200)}


def _directory_source(item, title):
    """Listeleme sayfasının bir firma adı olarak sunulmasını önler."""
    lowered = title.casefold()
    return (item.get("directory_flag") is True
            or (lowered.startswith("top") and any(word in lowered for word in ("supplier", "manufacturer", "companies", "tedarikçi", "üretici")))
            or any(phrase in lowered for phrase in ("supplier directory", "suppliers directory", "tedarikçi rehberi", "üretici rehberi")))


def validate_research(research_text, rows):
    """Araştırma çıktısını daraltır; kaynak id'si, tarih ve bağlantı kontrol edilir."""
    research = _json_section(research_text, RESEARCH_MARKER)
    if research is None:
        return None, {}, ["Araştırma verisi yok veya araştırma biçimi geçersiz; web araştırması doğrulanamadı."]
    as_of = _parse_date(research.get("as_of"))
    profile = research.get("profile")
    materials = research.get("materials")
    if as_of is None or as_of > date.today() or not isinstance(profile, dict) or not isinstance(materials, list) or len(materials) > 100:
        return None, {}, ["Araştırmanın tarih/profil/malzeme bilgisi geçersiz; stratejik öneri üretilemedi."]
    region = profile.get("region")
    if not isinstance(region, str) or region.strip().casefold() not in {"türkiye", "turkiye", "tr", "turkey"}:
        return None, {}, ["Araştırma bölgesi Türkiye olarak doğrulanamadı; stratejik öneri üretilemedi."]
    lookback = profile.get("lookback_days", 30)
    if type(lookback) is not int or not 1 <= lookback <= 90:
        return None, {}, ["Araştırma tarih aralığı geçersiz; stratejik öneri üretilemedi."]
    # Eski bir raporun kendi as_of tarihi, eski kaynakları güncel saydırmaz.
    earliest = max(as_of, date.today()) - timedelta(days=lookback)
    codes = {str(row["malzeme_kodu"]) for row in rows}
    selected_codes = _selection_codes(profile)
    normalized, candidates = [], []
    rejected = 0
    for material in materials:
        if (not isinstance(material, dict) or not isinstance(material.get("material_code"), str)
                or material["material_code"] not in selected_codes):
            rejected += 1
            continue
        normalized_material = {"material_code": material["material_code"], "name": _bounded_text(material.get("name"), 200), "suppliers": [], "news": []}
        for kind in ("suppliers", "news"):
            items = material.get(kind, [])
            if not isinstance(items, list) or len(items) > 100:
                rejected += 1
                continue
            for item in items:
                if not isinstance(item, dict):
                    rejected += 1
                    continue
                evidence_id = _bounded_text(item.get("evidence_id"), 120)
                title = _bounded_text(item.get("title"), 300)
                url = _public_url(item.get("url"))
                retrieved = _parse_date(item.get("retrieved_at"))
                if not evidence_id or not title or not url or retrieved is None or not earliest <= retrieved <= as_of:
                    rejected += 1
                    continue
                source = {"evidence_id": evidence_id, "material_code": material["material_code"], "kind": kind,
                          "title": title, "url": url, "retrieved_at": retrieved.isoformat()}
                if kind == "suppliers":
                    if item.get("verification") != "search_candidate" or item.get("existing_supplier_match") is not False:
                        rejected += 1
                        continue
                    source["name"] = _bounded_text(item.get("name"), 200) or title
                    source["directory_flag"] = _directory_source(item, title)
                else:
                    published = _parse_date(item.get("published_at"))
                    publisher = _bounded_text(item.get("publisher"), 200)
                    if (item.get("verification") != "headline_only" or published is None or not earliest <= published <= retrieved
                            or (item.get("publisher") is not None and not publisher)):
                        rejected += 1
                        continue
                    source.update(published_at=published.isoformat(), publisher=publisher)
                candidates.append((normalized_material, kind, source))
        normalized.append(normalized_material)
    counts = Counter(source["evidence_id"] for _, _, source in candidates)
    sources = {}
    for material, kind, source in candidates:
        if counts[source["evidence_id"]] != 1:
            rejected += 1
            continue
        material[kind].append(source)
        sources[source["evidence_id"]] = source
    warnings = []
    if rejected:
        warnings.append(f"{rejected} kaynak/kayıt, kimlik, tarih, bağlantı veya uygunluk kontrolünü geçmedi; kullanılmadı.")
    raw_warnings = research.get("warnings", [])
    if isinstance(raw_warnings, list):
        warnings.extend(_markdown(text) for value in raw_warnings[:10] if (text := _bounded_text(value, 500)))
    clean = {"as_of": as_of.isoformat(), "status": research.get("status"), "profile": profile, "materials": normalized,
             "stock_codes": codes}
    return clean, sources, warnings


def validate_proposals(model_text, rows, sources, selected_codes=None):
    """Modelin miktar ve serbest anlatımı alınmaz; eylem yalnız kanıt kimliğiyle kabul edilir."""
    if isinstance(model_text, str):
        # Bazı modeller başlığın (JSON) son ekini atlar. Veri kuralları değişmez.
        model_text = model_text.replace("STRATEJİK ÖNERİLER\n", PROPOSALS_MARKER)
    parsed = _json_section(model_text, PROPOSALS_MARKER)
    if parsed is None:
        return [], int(isinstance(model_text, str) and PROPOSALS_MARKER in model_text)
    proposals = parsed.get("proposals")
    if not isinstance(proposals, list) or len(proposals) > 100:
        return [], 1
    codes = {str(row["malzeme_kodu"]) for row in rows} | (selected_codes or set())
    accepted, rejected, seen = [], 0, set()
    for proposal in proposals:
        if not isinstance(proposal, dict):
            rejected += 1
            continue
        code, action = proposal.get("material_code"), proposal.get("action")
        ids = proposal.get("evidence_ids")
        reason = _bounded_text(proposal.get("reason"), 1000)
        days = proposal.get("additional_days")
        if (not isinstance(code, str) or code not in codes or not isinstance(action, str)
                or action not in {"erken_siparis_incele", "izle", "teklif_al"}
                or not reason or not isinstance(ids, list) or not 1 <= len(ids) <= 10
                or any(not isinstance(item, str) or item not in sources or sources[item]["material_code"] != code for item in ids)
                or (days is not None and (type(days) is not int or days < 0 or days > 365))):
            rejected += 1
            continue
        unique_ids = list(dict.fromkeys(ids))
        # Teklif eylemi için haber başlığı tek başına tedarikçi kanıtı değildir.
        if action == "teklif_al" and not any(sources[item]["kind"] == "suppliers" for item in unique_ids):
            rejected += 1
            continue
        key = (code, action, tuple(unique_ids))
        if key not in seen:
            trigger = proposal.get("trigger", "diger")
            if not isinstance(trigger, str) or trigger not in TRIGGER_LABELS:
                trigger = "diger"
            accepted.append({"material_code": code, "action": action, "evidence_ids": unique_ids, "trigger": trigger})
            seen.add(key)
    return accepted, rejected


def render_research(research_text, model_text, rows):
    research, sources, warnings = validate_research(research_text, rows)
    lines = ["", "Alternatif tedarikçi ve hammadde takibi"]
    if research is None:
        return lines + warnings
    lines.append("Araştırma tarihi: " + research["as_of"] + ". Bölge: Türkiye.")
    for material in research["materials"]:
        label = material["material_code"] + (" · " + material["name"] if material["name"] else "")
        lines.append("\n" + _markdown(label))
        if not material["suppliers"]:
            lines.append("- Alternatif tedarikçi adayı bulunamadı veya doğrulama kontrolünden geçmedi.")
        for source in material["suppliers"]:
            source_label = "Tedarikçi araştırma kaynağı" if source["directory_flag"] else "Tedarikçi adayı"
            source_note = " Listelenen firmalar ayrıca bulunmalı ve teyit edilmeli." if source["directory_flag"] else ""
            lines.append(f"- {source_label}: [{_markdown(source['name'])}]({source['url']}) · {_markdown(source['evidence_id'])}. Teklif ve teknik uygunluk doğrulanmadı.{source_note}")
        if not material["news"]:
            lines.append("- Tarihi doğrulanan güncel hammadde haberi bulunamadı.")
        for source in material["news"]:
            publisher_label = source["publisher"] or ("Yayıncı belirtilmedi (haber dizini) · " + urlsplit(source["url"]).hostname)
            lines.append(f"- Haber: {source['published_at']} · {_markdown(publisher_label)} · [{_markdown(source['title'])}]({source['url']}) · {_markdown(source['evidence_id'])}. Başlık düzeyinde kaynak; etkisi teyit edilmeli.")
    status = research.get("status")
    if isinstance(status, str) and status in {"ok", "completed"}:
        lines.append("Araştırma durumu: tamamlandı.")
    else:
        status_label = {"partial": "kısmi", "awaiting_selection": "malzeme ve tedarikçi seçimi bekleniyor", "error": "erişim hatası", "empty": "sonuç yok"}.get(status) if isinstance(status, str) else None
        lines.append("Araştırma durumu: " + (status_label or _markdown(_bounded_text(status, 100) or "belirsiz")) + "; kapsamlı web kontrolü tamamlandı kabul edilmez.")
    if not research["materials"]:
        lines.append("Takip edilecek hammadde/malzeme ve mevcut tedarikçi bilgisi eksik; araştırma yapılamadı.")
    lines.extend(warnings)
    proposals, rejected = validate_proposals(model_text, rows, sources, _selection_codes(research["profile"]))
    if proposals:
        lines.append("\nKaynaklara bağlı inceleme önerileri")
    action_text = {"erken_siparis_incele": "Erken sipariş ihtiyacını inceleyin", "izle": "Hammadde gelişmesini izleyin", "teklif_al": "Alternatif tedarikçiden teklif alın"}
    for proposal in proposals:
        refs = ", ".join(f"[{_markdown(item)}]({sources[item]['url']})" for item in proposal["evidence_ids"])
        action_label = action_text[proposal["action"]]
        supplier_refs = [sources[item] for item in proposal["evidence_ids"] if sources[item]["kind"] == "suppliers"]
        if proposal["action"] == "teklif_al" and supplier_refs and all(source["directory_flag"] for source in supplier_refs):
            action_label = "Araştırma kaynaklarından tedarikçi belirleyip teklif alın"
        lines.append(f"- {_markdown(proposal['material_code'])}: {action_label}. İnceleme önerisi; kanıt: {refs}.")
        if proposal["action"] == "erken_siparis_incele":
            lines.append("  Değerlendirme: " + TRIGGER_LABELS[proposal["trigger"]] + " bakımından kaynaklar incelenmeli (model yorumu); gelişmenin gerçekleştiği ve bu malzemeye etkisi ayrıca teyit edilmeli.")
            lines.append("  Miktar belirlenmedi. Bütçe, raf ömrü, teknik şartname, asgari sipariş miktarı (MOQ), depo kapasitesi ve tedarikçinin teslim/fiyat teyidi gerekir. Yerel stok tablosundaki temel miktar değiştirilmedi.")
            if proposal["material_code"] not in research["stock_codes"]:
                lines.append("  Bu araştırma kalemi için stok/tüketim/termin verisi yok; miktar hesaplanamaz.")
    if rejected:
        lines.append(f"{rejected} model önerisi kaynak/malzeme/biçim kontrolünü geçmedi; yayımlanmadı.")
    if not proposals:
        lines.append("Kaynağı doğrulanan stratejik öneri yok; erken sipariş miktarı belirlenmedi.")
    return lines


def validate_model_table(rows, model_text):
    """Yalnız karar sütunlarını doğrular; serbest model açıklamaları yayımlanmaz."""
    codes = {str(row["malzeme_kodu"]) for row in rows}
    parsed = []
    for line in model_text.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [part.strip().replace("**", "") for part in line.strip().split("|")[1:-1]]
        if cells and cells[0] in codes:
            parsed.append(cells)
    if len(parsed) != len(rows):
        return False
    expected, actual = [], []
    for row in rows:
        expected.append(tuple(_cell(row.get(field)).casefold() for field in FIELDS))
    for cells in parsed:
        if len(cells) != len(FIELDS):
            return False
        normalized = []
        for index, value in enumerate(cells):
            if index in (2, 3, 4, 5) and value.casefold() in {"null", "none"}:
                value = "—"
            if index in (2, 3, 4, 5) and value not in {"—", "-", ""}:
                try:
                    number = Decimal(value.replace(",", "."))
                    if not number.is_finite():
                        return False
                    value = str(int(number)) if number == number.to_integral_value() else format(number.normalize(), "f")
                except InvalidOperation:
                    return False
            elif index in (2, 3, 4, 5) and value in {"-", ""}:
                value = "—"
            normalized.append(value.casefold())
        actual.append(tuple(normalized))
    return Counter(expected) == Counter(actual)


class VerifiedStockTableComponent(Component):
    display_name = "Kaynakla Doğrulanan Son Tablo"
    description = "Yerel stok değerlerini korur; güncel web kaynaklarını ve kanıtlı inceleme önerilerini gösterir."
    name = "VerifiedStockTable"
    icon = "Table"
    inputs = [
        MessageTextInput(name="source_data", display_name="Yerel hesap ve kaynak", required=True),
        MessageTextInput(name="model_output", display_name="Claude kontrol çıktısı", required=True),
        MessageTextInput(name="research_data", display_name="Tedarikçi ve hammadde araştırması", value="", required=False),
    ]
    outputs = [Output(name="report", display_name="Doğrulanmış tablo", method="build_report")]

    def build_report(self) -> Message:
        source = _text(self.source_data)
        if MARKER not in source:
            raise ValueError("Yerel hesap çıktısı bulunamadı; kesin tablo üretilemez")
        report = json.loads(source.rsplit(MARKER, 1)[1])
        if report.get("errors"):
            self.status = "Girdi hatası; kesin satın alma önerisi yok"
            return Message(text="Girdi kontrolü gerekli:\n" + "\n".join(str(e) for e in report["errors"]))
        rows = report.get("rows", [])
        if not rows:
            raise ValueError("Yerel hesap çıktısında veri satırı yok")
        matched = validate_model_table(rows, _text(self.model_output))
        self.status = "Model tablosu yerel hesaplarla eşleşti" if matched else "Model tablosu eşleşmedi; yerel hesaplar gösteriliyor"
        header = "| Malzeme | Birim | Net stok | Hedef stok | Zamanında gelecek sipariş | Önerilen miktar | Durum |"
        lines = [header, "|---|---|---:|---:|---:|---:|---|"]
        for row in rows:
            lines.append("| " + " | ".join(_cell(row.get(field)) for field in FIELDS) + " |")
        lines.append("\nKontrol: " + self.status + ".")
        lines.extend(render_research(_text(getattr(self, "research_data", "")), _text(self.model_output), rows))
        return Message(text="\n".join(lines))
