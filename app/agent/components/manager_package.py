"""Yerel doğrulanmış karşılaştırmayı araştırma kaynaklarıyla yönetici taslağına birleştirir."""

from lfx.custom import Component
from lfx.io import MessageTextInput, Output
from lfx.schema.message import Message

QUOTE_RESULT_MARKER = "TEKLİF KARŞILAŞTIRMASI (JSON)\n"
NEGOTIATION_RESULT_MARKER = "PAZARLIK TASLAĞI (JSON)\n"


def _package_text(value):
    return value.text if isinstance(value, Message) else str(value or "")


class ProcurementManagerPackage(Component):
    display_name = "Yönetici Karşılaştırması"
    description = "Teklif e-postası, teknik özellik, vade farkı ve KDV kıyasını araştırma kaynaklarıyla sunar."
    name = "ProcurementManagerPackage"
    icon = "FileCheck"
    inputs = [
        MessageTextInput(name="quote_report", display_name="Kaynaklı teklif karşılaştırması", required=True),
        MessageTextInput(name="research_report", display_name="Kontrol edilmiş araştırma", required=True),
        MessageTextInput(name="negotiation_draft", display_name="Kontrol edilmiş pazarlık taslağı", value=""),
    ]
    outputs = [Output(name="report", display_name="Yönetici raporu taslağı", method="build_report")]

    def build_report(self) -> Message:
        self.status = "Yönetici raporu taslağı; gönderilmedi"
        quote_text = _package_text(self.quote_report).split(QUOTE_RESULT_MARKER, 1)[0].rstrip()
        text = quote_text + "\n\nAraştırma ve stok değerlendirmesi\n\n" + _package_text(self.research_report)
        negotiation = _package_text(getattr(self, "negotiation_draft", "")).split(NEGOTIATION_RESULT_MARKER, 1)[0].strip()
        if negotiation:
            text += "\n\nPazarlık önerisi ve yazışma taslağı\n\n" + negotiation
        return Message(text=text)
