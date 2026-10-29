# Satın alma agentı · hedef kurulum kuralları

Türkçe iletişim kur. Bu klasör yalnız hammadde/sarf satın alma pilotudur. Diğer agent projelerini, kişisel Outlook'u veya önceki bilgisayardaki akış UUID'sini kullanma.

İlk iş `START_HERE.md`, `README.md`, `Kurulum_Gorevi.md`, `Kurulum_ve_Guncelleme.md` ve manifesti oku. Paket kaynakları değişmez kabul edilir; ayar, kuyruk, Langflow veritabanı, log ve raporları paket dışında tut. Langflow/kuyruk worker komutlarında `SATINALMA_CONFIG` zorunludur. Ayrı SAP hazırlık/okuma CLI'si yalnız kendi açık `--config` dosyasını kullanır; kuyruk yazmaz. Kaynakta kalan configsiz geliştirme modu hedef kurulum modu değildir.

Bağlantısız `agent/sap_schema_review.py` ayrı bir dosya incelemesidir: `--metadata` ve `--output`, isteğe bağlı tek `--entity-set` kullanır; config, kimlik veya servis istemez. Envanter raporunu canlı bağlantı, alan eşlemesi veya iş kuralı kabulü sayma. Gerçek metadata/raporu kaynak pakete ekleme.

Paket worker'larında eksik config durdurma koşuludur; geliştirme modunu kullanarak geçme. Kaynak testlerini `transfer/run_offline_tests.py` ile geçici sentetik kökte çalıştır. Yardımcı yalnız test sürecini hazırlar; gerçek paket modunun config zorunluluğunu kaldırmaz.

Sırları okuma/yazdırma/taşıma. Gizli değerleri kullanıcı güvenli yerel alanda girsin. Güncelleme öncesi tutarlı veritabanı ve bütün rapor/ek dosyalarını yedekle. Bilinmeyen servisi/port sahibini kapatma. Mevcut veritabanına test verisi yazma.

Süre kullanıcı tarafından saat dilimiyle doğrulanmadan araştırma açılmaz. RFQ yanıtı kesin konuşma/malzeme kaynağına bağlanır. Fiyat/KDV/vade/teknik özellik uydurulmaz. Araştırma ve pazarlık dış yazışmaları yerel taslaktır; gönderim, ticari kabul, sipariş ve SAP'ye yazma kapsam dışıdır.

Yeni `agent/sap_diagnostics.py` şema ve tek kalem tanısıdır; talep JSON'u/kuyruk üretmez ve iş semantiğini onaylamaz. Ham API anahtarını birebir koru; gösterim sıfırlarını otomatik silme. Kaynak GUI okuması hedef adapter kabulünün yerine geçmez. `sap/pr_field_contract.json` içindeki boş kaynak eşlemelerini varsayarak doldurma. Kısmen siparişli veya kapsamı/kişi/durumu belirsiz kaydı araştırmaya alma.

İlk kurulum, bağımlılık uyumu, akış importu, gerçek API erişimi, model çalışması, yeniden başlatma, yedek geri dönüş ve uzak erişimi ayrı kanıtla. Paket hash'i ve yerel test sonucu hedef kabulünün yerine geçmez. Hedef kurulum raporunu paket dışında tut; sır veya kişisel veri yazma.
