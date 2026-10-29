# Satın alma agentı · repo çalışma kuralları

Türkçe, açık ve kanıta dayalı iletişim kur. Bu repo yalnız hammadde ve sarf satın alma agentına aittir. Mesaj yönlendirme, dijital ofis, ayrı SAP stok/PO paneli veya başka agent projelerini buraya dahil etme.

Önce kök `README.md`, `docs/DEVIR.md`, ardından `app/AGENTS.md` ve `app/README.md` dosyalarını oku. Git durumunu kontrol et. Uygulama rehberlerindeki paket kökü `app/` klasörüdür; oradaki komutları repo kökünden yanlış göreli yolla çalıştırma.

## Kaynak ve değişiklik sınırı

- `app/`, 0.3.0 kaynak tesliminin içeriğidir. Bu devirde işlevsel kaynaklar korunur. Kök Git metadata'sı uygulama manifestinin dışında kalır.
- `app/transfer/verify_package.py`, yalnız `app/` dosyalarını ve manifestini tam envanterle kontrol eder. Yeni dosya/cache veya değişen hash nedeniyle reddi görmezden gelme; manifesti farkı gizlemek için düzenleme.
- Sonraki işlevsel geliştirmede ayrı dal, yeni kaynak sürümü, açık dosya seçimi, güncel hash envanteri ve temiz checkout/çıkarım testleri birlikte hazırlanır. Test veya güvenlik kapısı gevşetilmez.
- Mevcut servisleri, port sahiplerini veya başka projenin veritabanını durdurma/değiştirme. Yalnız gerekli mevcut araçları kullan; bağımlılık kurmadan önce kurulu ortamı kontrol et.

## Ayarlar, veri ve sırlar

- Gerçek config/state/DB/log/rapor, kurumsal metadata, sertifika ve sırlar repo dışında kalır. Gerçek e-posta/teklif/veri örneğini fixture olarak ekleme.
- Anahtar/parola, Keychain veya `.env` değerlerini okuma/yazdırma. Kullanıcı sırları güvenli yerel alanda girsin. JSON ve Git'e yalnız güvenli değişken adları veya boş şablonlar alınır.
- Worker'larda `SATINALMA_CONFIG` gerekir; eksik config ile eski geliştirme yolu veya SQLite'a dönme. SAP CLI'si ayrı açık `--config` kullanır; şema/tanı raporu kuyruk girdisi değildir.
- `app/sap/pr_field_contract.json` içindeki 12 boş alan eşlemesini varsayımla doldurma. API anahtarını birebir koru; sıfır silme, String→Boolean veya talep sahibi e-posta tahmini yapma.

## İş kuralları ve kabul

- Kesin tarih/saat/saat dilimi talep sahibinden alınmadan araştırma açılmaz. Teklif kaynak message/attachment ve aynı malzeme kapsamıyla desteklenir; belirsiz kayıtlar birleştirilmez.
- Fiyat/KDV/vade/teknik özellik/geçmiş alış üretilmez. Kısmen siparişli veya kişi/durum/kapsamı belirsiz SAP kalemi araştırmaya alınmaz.
- E-posta, RFQ ve pazarlık çıktıları insan incelemesine sunulan yerel taslaklardır. Gönderim, ticari kabul, sipariş veya SAP iş verisi yazma başarı iddiası gerçek ayrı kanıt olmadan yapılmaz.
- Otomatik posta servisi, bağımsız zamanlayıcı ve çok kullanıcılı onay paneli tamamlanmış sayılmaz. Kaynak GUI/yerel test/alan doluluğu, hedef bağlantı kabulü değildir.

Python komutlarını `uv run` ile, uygulama testlerini geçici sentetik state ile çalıştır. `PYTHONDONTWRITEBYTECODE=1` ve pytest `-p no:cacheprovider` kullanarak manifestli kaynağa cache yazma. Güncel doğrulama sonucunu, kapsamını ve kalan engeli belirt; tarihli 806 test sonucunu yeni çalışma gibi sunma. Kabul kaydını `app/Kabul_Kaydi_Sablonu.md` kopyasıyla repo dışında tut.

GitHub'a yazma/paylaşma gerektiğinde hesap, repo, görünürlük ve alıcıyı doğrula. Bu reponun herkese açık okuma/indirme erişimi ile doğrudan yazma/collaborator yetkisini ayır. Yeni yazma davetinde gönderilen davet ile kabul edilmiş erişimi ayrı raporla. Kişisel veri veya ileti içeriğini proje notuna kaydetme.
