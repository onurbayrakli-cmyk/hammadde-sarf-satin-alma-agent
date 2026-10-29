# BT/Basis teslimi · satın alma talebi okuma pilotu 0.3.0

İstenen sonuç, izinli **hammadde/sarf** kapsamındaki tek SAP satın alma talebi kalemini salt okunur alıp doğru talep sahibi ve iş durumu ile kabul etmektir. Paket SAP servislerini kurmaz, yeniden yayımlamaz ve teknik kullanıcı oluşturmaz. Ayrı stok/PO paneli veya başka agentların bağlantıları bu agentın kabul kanıtı değildir.

## Devir özeti

Kaynak bilgisayarda, kullanıcının DEV oturumunda standart PR servis kökü ve tam metadata HTTPS GET / 200 OK verdi. Beş veri kümesi ve kalem kümesinin iki String anahtarı/yedi temel alanı/toplam 18 alanı yapısal olarak incelendi. Seçilen tek kalem gerçek API anahtarıyla birebir eşleşti. Bu GUI kaynak okumasıdır; Python adapterinin bağımsız TLS/kimlik/kodlama veya yeni Mac mini kabulü değildir.

Örnek kısmen siparişli olduğundan ilk pilot intake koşulunu sağlamadı; kuyruğa alınmadı. Kaynak ekran karşılaştırması, durum/kişi/kapsam anlamları ve teknik salt okunur yetki açık kaldı. Alan sözleşmesindeki 12 `confirmed_source_field` değeri boştur. Kurum endpoint/client'ı, gerçek XML/iş satırı, kullanıcı, sertifika ve kimlik bilgileri tarafsız ZIP'e konulmamıştır; BT bunları ayrı güvenli kanaldan sağlar.

## BT'den sağlanacak bilgiler

| Konu | Bilgi ve kabul kanıtı |
|---|---|
| Ortam | DEV/QA/PRD, client, mantıksal kaynak sistem takma adı ve sorumlu kişi. Test ile canlı ayrılır. |
| PR kaynağı | Gerçek servis/sürüm/EntitySet ve tam metadata gövdesi. Standart aday `API_PURCHASEREQ_PROCESS_SRV` / `A_PurchaseRequisitionItem`; isim varlığı uyumluluk kanıtı değildir. Malzeme/stok/PO/raporlama listesi operasyonel PR sözleşmesi yerine geçmez. |
| Ağ | Yeni Mac mini için VPN, şirket DNS'i/iç rota ve gerekli hedef IP/portların dar izin listesi. SAP'nin internete açılması istenmez. |
| TLS | Özgün hostname ve geçerli zincir; gerekiyorsa onaylı CA bundle. Kontrol kapatılmaz; özel anahtar/PFX kaynak pakete eklenmez. |
| Kimlik | İlk kontrollü testte kullanıcı hesabı kullanılabilir; salt okunur olduğu varsayılmaz. Sürekli işleme geçmeden teknik hesap, güvenli sır teslimi ve hesap yaşam döngüsü gerekir. SSO/OAuth edinimi/mTLS ayrı geliştirme olabilir. |
| Yetki | Gereken PR/plant/satın alma grubu okumaları; kapsam dışı organizasyon için olumsuz okuma testi. Yazma/onaylama/silme/sipariş yetkisi gerekmez. GET kullanan istemci sunucu yetkisini kısıtlamaz. |
| İş kapsamı | Onaylı tesis/organizasyon/malzeme grupları ve hammadde/sarf izin listesi. Açıklamadan sınıflandırma tahmin edilmez; kuralın intake hattında gerçekten uygulanması doğrulanır. |
| Durum | Silinme/kapanma/blokaj/onay alanlarının gerçek değerleri ve Boolean dönüşümünün kabul edilmiş iş anlamı. |
| Kişi | Gerçek PR sahibinin açık e-posta alanı veya kontrollü kurumsal eşleme. Teknik/ortak kullanıcı otomatik talep sahibi sayılmaz. |
| Anahtar | GUI gösterimi/API kaynak anahtarı farklıysa kaynak DDIC/conversion açıklaması. Adapter ham API anahtarını birebir korur; sıfır silinmez. |
| Koleksiyon | Tek kalem filtreleme, sayfalama/delta/count ve eksik/iptal/değişmiş kaynak davranışı. İlk pilot toplu tarama veya otomatik sayfa takibi yapmaz. |
| İşletim | Operatör/sorumlu, destek yolu, sır yenileme, özel kanıt saklama ve hedef yedek/geri dönüş politikası. |

Parola/token sohbet, e-posta, kaynak dosyası veya kabul kaydına yazılmaz. Kimlik yöntemi desteklenmiyorsa güvenliği düşürerek devam edilmez.

## Araçlar ve sınırları

[SAP README](README.md) gerçek CLI komutlarını içerir. Standart PR adları hazır ancak bağlantısı kapalı `connection.pr-api.candidate.json` veya genel `connection.example.json` şablonu kullanılır; ikisi de paket dışına kopyalanıp BT tarafından tamamlanır.

| Araç | İşlem | Kanıtlamadığı |
|---|---|---|
| `agent/sap_schema_review.py` | Kaydedilmiş tam XML'i ağ kullanmadan kataloglar. | Canlı güncellik, kimlik/yetki ve iş anlamı. |
| `agent/sap_diagnostics.py schema` | Adapter ile canlı metadata'yı okur/kataloglar. | Tek iş kalemi, intake sözleşmesi ve uygunluk. |
| `agent/sap_diagnostics.py sample` | Metadata ve seçilmiş tek PR/API kalemini sabit alanlar/`$top=2` ile okur. | Durum dönüşümü, kişi çözümleme, izinli iş kapsamı, intake veya kuyruk. |
| `agent/sap_worker.py status` | Harici ayar durumunu yerelde raporlar. | DNS, SAP erişimi veya kimlik/yetki. |
| `agent/sap_worker.py check/read` | Tamamlanmış eşleme ile tip/anahtar sözleşmesi ve tek kalem kabulünü sınar. `read` yeni kaynak JSON'u üretir. | Kurumsal yetki/iş semantiği kabulü ve otomatik uçtan uca kuyruk. |

Tanı çıktıları rapordur; `source_file_created=false`, `queue_written=false`, `sap_written=false`, `outbound_sent=false` kalır. Tanı raporu `ingest-request` girdisi değildir. Başarısız denemede `performed_api_call=null`, API'ye hiç gidilmediği anlamına gelmez; otomatik tekrar döngüsü başlatılmaz.

## Alan sözleşmesi ve durma koşulları

Worker tam PR+kalem iki anahtarı ve 12 doğrudan alan ister. `pr_field_contract.json` dokümantasyon sözleşmesidir; runtime ayarı/otomatik mapping değildir. `field_map` yalnız ad eşler: String göstergeleri Boolean'a çevirmez, `ReleaseIsNotCompleted` değerini terslemez ve kullanıcı kodundan e-posta üretmez. Gerekli semantik alanlar yoksa kontrollü salt okunur katman tasarlanır; kaynak DCL/yetkisinin özel entity'ye otomatik devralındığı varsayılmaz.

İlk pilotun koşulları:

- Malzeme kodu/açıklama, pozitif istenen miktar ve kaynak birimi açık olmalı.
- `OrderedQuantity=0`, `deleted=false`, `closed=false`, `blocked=false`, `released=true` olmalı.
- Kaynak sistem + PR + ham API kalem anahtarı ve tek/tam kayıt korunmalı.
- Teyitli talep sahibi e-postası, onaylı iş kapsamı ve teknik okuma yetkisi doğrulanmalı.
- Araştırma bitişi gerçek talep sahibinden saat dilimiyle ayrıca alınmalı; SAP oluşturma/teslim tarihi kullanılmamalı.

Kısmen siparişli, eksik kişi/durum, belirsiz anahtar, kapsam dışı veya eksik koleksiyon incelemede kalır. İstenen miktardan sipariş miktarı çıkarılıp ihtiyaç oluşturulmaz. Tanı başarısı bu kapıları kaldırmaz.

## Mac mini hedef kabul sırası

1. Paket hash/manifest ve çevrimdışı testlerini doğrulayın; hedef config/özel kanıt dizinini paket dışında oluşturun.
2. Aynı SAP sistemi kullanılacaksa mevcut servisi teyit edin; bilgisayar değiştiği için servis/ICF/paket kaydı oluşturmayın.
3. VPN/DNS/hostname/TLS ve dar ağ izin listesini doğrulayın. HTTP401'i TLS başarısı ve kimlik eksikliği olarak ayırın; iş okuması saymayın.
4. Harici SAP config'ini tamamlayın; sırları güvenli hedef ortamından sağlayın. `status` sonrası kontrollü canlı `schema` tanısı çalıştırın.
5. Yetkili kişinin seçtiği bir PR ve gerçek API kalem anahtarıyla `sample` çalıştırın. Kaynak ekranla kimlik/miktar/birim/durum/kişi karşılaştırın. GUI/adapter yanıt kodlamasını ayrıca doğrulayın.
6. Alan/Boolean/kişi/kapsam tasarımını tamamlayın. Eksik dönüşüm veya zorunlu kapsam kapısı uygulaması geliştirme görevi olarak kalır; varsayımla kapatılmaz.
7. Teknik salt okunur hesapla izinli tek kalem ve kapsam dışı organizasyon/eksik yetki için olumsuz okuma kabulünü yapın. SAP'ye yazan olumsuz test çalıştırmayın.
8. Silinmiş/kapalı/bloke/onaysız/pozitif sipariş miktarı, eksik kişi ve eksik koleksiyonun intake üretmediğini doğrulayın; kaynak kanıtını koruyun.
9. Tam sözleşme sonrası `check/read` ile kaynak dosyası oluşturun. Manuel kuyruk kabulü ayrı insan adımıdır; mevcut ingest `sap_read_evidence` ekini otomatik doğrulayıp DB'ye kalıcılaştırmaz. Otomatik hat için bu eksik giderilir.
10. Süre onayı, iki sağlayıcıyla sınırlı gerçek çalışma ve insan onaylı posta akışını ayrı kabul edin. Tekrar işleme, kaynak değişikliği ve yedekten dönüşü gerçek sipariş oluşturmadan sınayın.

## SAP tarafı değişiklik ve teslim sınırı

İstemci Mac mini'de, servis SAP sunucusunda çalışır. DEV'deki yerel nesne seçimi geçici/yalıtılmış/salt okunur ortam değildir. QA/PRD için paket/transport, SICF/HTTPS/kimlik, yetki ve hedef iş kabulü ayrıca planlanır. OAuth kutusu veya varsayılan client gibi GUI ayarlarından anonim erişim/izolasyon sonucu çıkarılmaz.

Kabul formunda paket sürümü/hash'i, ortam takma adı, tarih, kontrol sahibi, metadata/rapor hash'leri, hesap türü, kapsam ve geçen/kalan adımlar yer alsın. Teknik hesap veya üretime geçiş tamamlanmadan “canlı otomatik SAP entegrasyonu hazır” denilmez. Bu teslim SAP'ye yazma, sipariş, tedarikçiye gönderim veya indirim/ek vade kabulü açmaz.
