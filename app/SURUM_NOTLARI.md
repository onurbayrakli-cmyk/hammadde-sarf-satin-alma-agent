# Sürüm notları

## 0.3.0 · 22 Ekim 2026

Mac mini/macOS'a AirDrop ile aktarılacak **kaynak pilot kurulum kiti**. Kapsam hammadde ve sarf malzeme satın alma araştırması, mevcut/yeni tedarikçi teklifleri, yönetici karşılaştırması ve insan onaylı pazarlık taslaklarıdır.

- Ayrıntılı ana README, kısa başlangıç, Claude/Codex kurulum görevi, hedef kabul şablonu ve güncel SAP/BT devir rehberi eklendi/güncellendi.
- `agent/sap_diagnostics.py` ve sentetik testleri pakete eklendi. Şema ve seçilmiş tek kalem tanısı özel rapor oluşturur; talep JSON'u veya kuyruk üretmez.
- Standart PR API için hedef bağımsız, kapalı bağlantı taslağı eklendi. Origin, istemci, IP izinleri, kimlik referansları ve CA alanları boştur; otomatik bağlantı yoktur.
- Kaynak GUI'de doğrulanan metadata/tek kalem okuması ile hedef Mac/Python adapteri, iş semantiği ve intake kabulü ayrıldı. Ham API anahtarları korunur; gösterim sıfırları silinmez. Kaynak örnek kısmen siparişli olduğu için araştırmaya alınmamıştır.
- Önceki sürümün 21 düğüm / 28 bağlantılı temiz akışı ve dış config/state zorunluluğu korunur. İş kuralları, runtime, mevcut kuyruğun verileri ve SAP iş verisi bu teslim için değiştirilmedi.

### Açık kabul başlıkları

Mac mini üzerinde bağımlılık kurulumu ve akış importu, gerçek sağlayıcı/ortak iki-model koşusu, SAP adapter bağlantısı ve iş sözleşmesi, dar yetkili teknik hesap, ayrı bot kutusu, çok kullanıcılı onay paneli, bağımsız posta/zamanlayıcı ve yeniden başlatma/geri yükleme testleri ayrı tamamlanacaktır. Bu kit otomatik sipariş veya dış ileti göndermez.

Bağımlılık listesi adaydır; internet gerektirir. Python/uv/Langflow binary'leri, çevrimdışı wheel deposu veya bütün geçişli bağımlılıkları kilitleyen hedef lock dosyası içermez. Hedefte başarılı çözüm sonrası sürümler kaydedilir. Mac mini'de test edilmiş, üretime hazır veya imzalı uygulama paketi iddiası yoktur.

Kaynak paket kontrolünün gerçek test sayıları ve ZIP SHA-256 değeri ZIP'in yanında ayrı teslim doğrulama kaydında bulunur. Bu belge hedef testlerinin geçtiği anlamına gelmez.

## Önceki sürümler

0.2.1: hedef config eksikliğinde kapalı durma, bağlantısız metadata incelemesi ve temiz çıkarımda 782 çevrimdışı test doğrulaması. Eski sürümler değiştirilmeden korunur; yeni kurulum için güncel ZIP ve ayrı hash kaydını kullanın.
