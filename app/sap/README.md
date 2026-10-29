# SAP satın alma talebi bağlantısı · Mac mini 0.3.0

Amaç, izinli **hammadde ve sarf malzemesi** satın alma taleplerini SAP'den salt okunur almak ve insan tarafından onaylanmış araştırma sürecine hazırlamaktır. Bu klasör kaynak kodu ve bağlantı şablonlarını içerir. Kurum endpoint'i, kullanıcı, parola, sertifika, gerçek metadata ve gerçek talep verisi pakete eklenmemiştir. Kurulum ve izole Python ortamı için önce [ana README](../README.md) okunmalıdır.

## 1. Devir anındaki durum

| Kontrol | Kaynak bilgisayarda gözlenen | Yeni Mac mini'de gereken |
|---|---|---|
| Standart PR servisi | DEV'de kullanıcı hesabıyla servis kökü ve tam metadata için HTTPS GET / 200 OK görüldü. | Aynı servis kullanılıyorsa yeniden kayıt oluşturmayın; hedef ağ, kimlik ve erişimi doğrulayın. |
| Şema | Beş veri kümesi kataloglandı; kalem kümesinde iki String anahtar ve toplam 18 seçili alan yapısal kontrolden geçti. | Hedef sistemin güncel tam metadata'sıyla yeniden kontrol edin. |
| Tek kalem | Kullanıcının seçtiği bir kalem kaynak API anahtarıyla sınırlı GET sonucunda birebir eşleşti. | Agent adapteriyle ayrı metadata ve tek kalem tanısı çalıştırın; kaynak ekranla karşılaştırın. |
| Satın alma uygunluğu | Örnek kısmen siparişliydi; kuyruğa alınmadı. | Sıfır sipariş miktarı, kapsam, durum ve gerçek talep sahibini doğrulayın. |
| Alan eşlemesi | 12 `confirmed_source_field` değeri boş. | İş anlamı yetkili kişi tarafından kabul edilmeden doldurmayın. |
| Otomasyon | SAP intake, araştırma, posta veya sipariş başlatılmadı. | Kabul kapıları tamamlanmadan açmayın. |

SAP GUI'deki başarılı yanıt, ayrı Python adapterinin TLS/kimlik/yanıt kodlamasıyla çalıştığını veya Mac mini'nin kabul edildiğini kanıtlamaz. Kullanıcı hesabıyla yapılan test, hesabın sunucu tarafında salt okunur olduğunu göstermez. Düzenli kullanım için dar kapsamlı teknik hesap ve olumsuz yetki testi gerekir.

Standart servis yazma işlemlerini de destekleyebilir. İstemcinin GET kullanması sunucu yetkilerini daraltmaz. SAP servis kaydı yeni bilgisayara ZIP ile taşınmaz; SAP tarafında kalır. QA/PRD yayını ve transport ayrı kurumsal işlemdir.

## 2. SAP erişimi olmadan yerel metadata incelemesi

`agent/sap_schema_review.py`, daha önce kaydedilmiş **tam metadata XML yanıt gövdesini** inceler. SAP, model, web, e-posta veya kuyruk çağırmaz. EntitySet → EntityType bağlarını, doğrudan alan tiplerini, anahtarları ve aggregate/parameters/inheritance uyarılarını listeler. Otomatik eşleme yapmaz.

Komutlar paket kökünden çalıştırılır. `SATINALMA_RUNTIME`, ana kurulumda oluşturulan izole Python ortamının mutlak yoludur. `SAP_METADATA_FILE` yalnız XML gövdesinin mutlak yolu; `SAP_SCHEMA_REPORT` paket dışında, mevcut özel dizinde henüz bulunmayan JSON dosyasının mutlak yoludur. Değişkenleri hedef dosyalarınıza göre tanımlayın; sır içermesinler.

```sh
uv run --no-project --no-sync --python "$SATINALMA_RUNTIME/bin/python" python agent/sap_schema_review.py --metadata "$SAP_METADATA_FILE" --output "$SAP_SCHEMA_REPORT"
```

İsteğe bağlı `--entity-set A_PurchaseRequisitionItem` yalnız bu ad gerçek XML'de bulunuyorsa kullanılabilir. Benzer isimli servis aynı sözleşme sayılmaz. Boş/kesilmiş XML, DTD/entity, belirsiz şema ve 1 MiB üzeri girdi reddedilir; dış XML referansları indirilmez. Rapor create-only ve 0600 izinlidir; eski rapor ezilmez.

`catalog.metadata_verified=true` yalnız yerel dosyanın yapısal incelemesidir. `contract_evaluated=false`, `sap_connection_verified=false` ve `business_semantics_verified=false` kalır. HTTP başlıkları/cookie/token ve gerçek talep verisi metadata dosyasına konulmaz. Kaynak ortam, alınma zamanı ve yetkili kişi ayrı özel kabul kaydında tutulur.

## 3. Hedef bağlantı ayarları

Standart PR tanısı için `sap/connection.pr-api.candidate.json`, başka doğrulanmış servis için genel `sap/connection.example.json` şablonunu **paket dışında**, yalnız operatörün eriştiği dizine kopyalayın. Standart aday yalnız servis/EntitySet adlarını hazırlar; origin/client/izinli ağ/kimlik/CA boş ve bağlantı kapalıdır. `SAP_CONFIG` hedef dosyanın mutlak yolunu göstermelidir. İki şablon da `enabled=false` ile başlar; alan adlarının bulunması bağlantı kabulü değildir.

| Alan | Kaynağı / kuralı |
|---|---|
| `source_system` | DEV/QA/PRD ve client'ı ayıran kurumca kararlaştırılmış mantıksal ad. Örnek etiket gerçek hedefi doğrulamaz. |
| `origin` | BT'nin verdiği özgün HTTPS origin'i; kullanıcı/parola, sorgu veya servis yolu eklenmez. |
| `service_root`, `entity_set` | Gerçek metadata ile eşleşen servis kökü/kalem kümesi. Standart adaylar `/sap/opu/odata/sap/API_PURCHASEREQ_PROCESS_SRV/` ve `A_PurchaseRequisitionItem`; otomatik kabul yapılmaz. |
| `sap_client` | Hedef client'ı BT doğrular. Boş şablondan veya GUI varsayımından türetilmez. |
| `allowed_networks` | SAP'nin çözümlenen adreslerini kapsayan, BT onaylı dar IP/CIDR izin listesi. İnternetin tümünü izinli yapmayın. |
| `auth` | `none`, `basic` veya `bearer`; sırın kendisi yerine ortam değişkeni adları yazılır. Basic: `username_env`/`password_env`; Bearer: `bearer_env`. |
| `tls.ca_file` | Gerekiyorsa BT'nin güvenli kanaldan verdiği onaylı CA bundle'ın hedefteki yolu. Özel anahtar/PFX kaynak pakete eklenmez. |
| `field_map` | Tanı için iş alanı dönüşümü değildir; tam intake sözleşmesi kabul edilene kadar boş kalabilir. |
| `max_response_bytes`, `timeout_seconds` | Sınırlı yanıt ve bağlantı/okuma bütçeleri. DNS ve tüm HTTP başlık işlemleri için kesin toplam duvar saati sınırı garantisi yoktur. |

Kimlik bilgilerini yalnız hedefteki onaylı sır yönetiminden sağlayın. Sohbete, README'ye, kabuk geçmişine, JSON'a, Git'e veya kabul raporuna yazmayın. OAuth/SSO token edinimi ve mTLS hazır değildir; gerekiyorsa ayrıca geliştirilir. VPN tek başına API kabulü değildir. TLS doğrulaması kapatılmaz; global hosts/DNS/güven deposu gelişigüzel değiştirilmez.

Önce yerel ayar durumunu okuyun:

```sh
uv run --no-project --no-sync --python "$SATINALMA_RUNTIME/bin/python" python agent/sap_worker.py --config "$SAP_CONFIG" status
```

`status` sır değerlerini, DNS'i ve SAP'yi okumaz. Sonraki canlı tanılar için BT onaylı hedef/kimlik ve açıkça etkinleştirilmiş `enabled=true` gerekir. Etkinleştirme yalnız harici hedef config'inde yapılır; paket içi şablon değiştirilmez. Adapter özgün hostname, TLS zinciri ve izinli IP'leri denetler; GET dışında yöntem, redirect, otomatik tekrar ve sayfa takibi yapmaz.

## 4. Canlı şema ve tek kalem tanısı

`agent/sap_diagnostics.py`, tüm iş alanları henüz eşleşmiyorken **tanı raporu** üretir. `schema` yalnız metadata okur:

```sh
uv run --no-project --no-sync --python "$SATINALMA_RUNTIME/bin/python" python agent/sap_diagnostics.py schema --config "$SAP_CONFIG" --output "$SAP_LIVE_SCHEMA_REPORT"
```

`SAP_LIVE_SCHEMA_REPORT`, paket dışında mevcut özel dizindeki yeni JSON yoludur. Başarıda `status=sap_schema_diagnostic`, `metadata_read_verified=true`, `requisition_read_verified=false` beklenir. `operational_schema_verified=false`, kataloglamanın intake sözleşmesini sınamadığını gösterir.

Yetkili kişinin seçtiği tek PR ve **API'nin kaynak kalem anahtarı** ile:

```sh
uv run --no-project --no-sync --python "$SATINALMA_RUNTIME/bin/python" python agent/sap_diagnostics.py sample --config "$SAP_CONFIG" --pr "$SAP_PR" --item "$SAP_ITEM" --output "$SAP_SAMPLE_REPORT"
```

`SAP_PR` ve `SAP_ITEM` operatörün seçtiği kaynak anahtarlarıdır; `SAP_SAMPLE_REPORT` başka bir yeni özel JSON yoludur. Araç metadata'yı, sonra sabit alan seçimi/`$top=2` ile tek kalemi okur. Yedi temel alan gerekir: `PurchaseRequisition`, `PurchaseRequisitionItem`, `Material`, `PurchaseRequisitionItemText`, `RequestedQuantity`, `OrderedQuantity`, `BaseUnit`. Şemada varsa yalnız tanımlı 11 durum/organizasyon alanı eklenir. Kişi/e-posta aranmaz, durumlar dönüştürülmez.

Başarıda `status=sap_sample_diagnostic`, `requisition_read_verified=true`, `operational_schema_verified=true`, `collection_complete=true`, `collection_scope=single_pr_item` beklenir. Rapor kaynak kaydı/hash'leri içerdiğinden özel iş verisidir. Başarıda bile `contract_evaluated=false`, `business_semantics_verified=false`, `intake_eligible=not_evaluated`, `source_file_created=false`, `queue_written=false`, `sap_written=false`, `outbound_sent=false` kalır. **Tanı raporunu `ingest-request` girdisi olarak kullanmayın.**

### Anahtar gösterimi ve hata halinde durma

Kaynak testinde sıfırlı GUI kalem gösterimi ile sıfırsız API anahtarı farklıydı. Örneğin gösterim `00010`, API kaydı `10` olabilir; bunlar sentetik örneklerdir. İstemci eşleşmeyi metin olarak birebir sınar; sıfır silmez, sayıya çevirmez ve eşdeğerlik varsaymaz. `NonNegative` veya uzunluk bilgisi genel eşdeğerlik kanıtı değildir. Uyuşmazlıkta kaynağı inceleyin ve gerçek API anahtarını yetkili kişi açıkça yeniden seçsin; korumayı gevşetmeyin.

Boş/çift/yanlış kalem, sayfalama/delta/count veya beklenmedik alan/tip reddedilir. Başarısız tanıda `performed_api_call=null`, çağrının hiç yapılmadığı anlamına gelmez; deneme sonrası sonuç kesinleştirilememiş olabilir. Aynı çağrıyı otomatik tekrar döngüsüne sokmayın. Hata incelemesinde sır veya ham kimlik başlıkları paylaşmayın.

## 5. Tam intake için ayrıca gerekenler

Tanıdan sonra kurum yetkilisi [BT teslim notu](BT_Teslim_Notu.md) ve [alan sözleşmesi](pr_field_contract.json) üzerinden doğrular:

- Tek kaynak sistem + PR + API kalem kimliği korunur; aynı malzemenin farklı talepleri birleştirilmez.
- Operasyonel EntitySet doğrudan türünde tam iki anahtar PR/kalem bulunur. Aggregate `ID`, başlık, parametre veya inheritance otomatik düzleştirilmez.
- Malzeme kodu/açıklama, pozitif istenen miktar, birim, sipariş miktarı ve gerçek talep sahibinin teyitli e-postası gerekir. Kullanıcı kodundan e-posta üretilmez.
- Durumlar gerçek Boolean sözleşmeye uyar: `deleted=false`, `closed=false`, `blocked=false`, `released=true`. String/boşluk kendiliğinden Boolean olmaz. `ReleaseIsNotCompleted` ters yönlüdür; doğrudan `released` alanına bağlanmaz. `field_map` yalnız ad eşler; kod dönüştürmez/terslemez/kişi çözümlemez.
- İlk pilot `OrderedQuantity=0` ister. Kısmen siparişli kalem incelemede kalır; `RequestedQuantity - OrderedQuantity` ile otomatik kalan ihtiyaç üretilmez.
- Hammadde/sarf ve tesis/organizasyon/malzeme grubu izin listesi ayrıca doğrulanıp uygulanmalıdır. Metadata/tanı başarısı bu iş kapsamı kapısının uygulandığını kanıtlamaz.
- Teknik salt okunur hesap, kapsam dışı okuma reddi ve kaynak ekranla kimlik/miktar/birim/durum/kişi karşılaştırması tamamlanır.

12 alan eşlemesi kabul edilmemiştir. Eksikler giderilene kadar burada durulur; sahte e-posta veya varsayılan durum eklenmez. Gerekli semantik alanlar kaynakta yoksa kontrollü salt okunur katman ayrı iştir.

Kabul tamamlandıktan sonra tam sözleşme ve yeni intake kaynağı sınanabilir:

```sh
uv run --no-project --no-sync --python "$SATINALMA_RUNTIME/bin/python" python agent/sap_worker.py --config "$SAP_CONFIG" check
uv run --no-project --no-sync --python "$SATINALMA_RUNTIME/bin/python" python agent/sap_worker.py --config "$SAP_CONFIG" read --pr "$SAP_PR" --item "$SAP_ITEM" --output "$SAP_NEW_SOURCE"
```

`SAP_NEW_SOURCE` paket dışında yeni dosyadır. `check` metadata tip/anahtar sözleşmesini; `read` tam sözleşme ve tek kalem kabulünü sınar. Çıktı `source`, `request`, `sap_read_evidence` içerir; SAP, posta veya kuyruk yazılmaz. Kanıtı ayrı koruyun. Manuel `case_worker.py ingest-request --file ...` ayrıca insan adımıdır; mevcut ingest, `sap_read_evidence` ekini otomatik denetleyip DB'ye kalıcılaştırmaz. Bu eksik tamamlanmadan otomatik SAP→kuyruk hattı hazır sayılmaz.

Araştırma bitişini gerçek talep sahibi saat dilimiyle ayrıca onaylar. SAP oluşturma/teslim tarihi araştırma son tarihi değildir. Görsel `SAPProcurementInput` düğümü CLI adapterini otomatik çağırmaz; API kabulü ile görsel akış ayrı doğrulanır.

## 6. Sorun giderme ve kabul kaydı

| Gözlem | Kontrol |
|---|---|
| Config eksik/kapalı | Harici mutlak yol, şema ve `enabled` kararını kontrol edin. Paket şablonunu canlı ayar sanmayın. |
| DNS/timeout/izinli ağ | VPN, kurum DNS'i, rota ve çözümlenen IP'nin dar izin listesiyle eşleşmesini BT doğrulasın. |
| TLS/hostname/zincir | Doğru origin ve onaylı CA'yı doğrulayın; kontrolü kapatmayın. |
| HTTP 401 / 403 | TLS ile kimlik/yetki başarısını ayırın. Hesap ve client/servis/organizasyon yetkisini inceleyin. |
| HTTP 200, tanı reddi | İçerik türü/kodlama, tam metadata, exact anahtar, tek satır, alan tipleri ve koleksiyon bütünlüğünü inceleyin. GUI başarısı adapter uyumluluğu değildir. |
| Şema geçiyor, intake geçmiyor | Durum/kişi/kapsam/Boolean sözleşme ve sıfır sipariş miktarını kontrol edin; kapıları gevşetmeyin. |
| Dosya hatası | Paket dışında mevcut gerçek üst dizin, yeni dosya adı ve yazma izni kullanın. Rapor ezilmez; sembolik bağlantı kullanılmaz. |

Kabul kaydı; ortam takma adı, zaman, paket sürümü/hash'i, metadata/rapor hash'leri, hesap türü, sınanan kapsam ve geçen/kalan kontrolleri içersin. Sır ve gereksiz iş verisi içermesin. Özel kanıtlar kurumun onaylı yedek/erişim politikasına göre korunur; dağıtım ZIP'ine geri eklenmez. Bu kurulum SAP'ye yazma, sipariş, fiyat/vade kabulü veya e-posta gönderimi açmaz.
