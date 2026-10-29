# Mac mini üzerinde kurulum ve kabul

Okuyucu: bu Mac'te çalışan Claude/Codex veya kurulumu yapan teknik kişi. İlk hedef 1–5 kişilik şirket içi, insan onaylı pilotun tek operatörle doğrulanmasıdır. Kullanıcı onay paneli ayrı geliştirme işidir; Langflow editörüne erişim satın alma onay rolü sağlamaz.

## 1. Yerleşim ve ön kontrol

Mac mini şirket ağına/VPN'e erişebilmeli ve model sağlayıcılarına çıkabilmelidir. macOS sürümü, Apple Silicon/Intel mimarisi, mevcut uv/Python, boş disk, çalışan servisler ve port sahipliği hedefte kaydedilir. Apple sistem Python'una paket kurmayın. Bu paket Intel/ARM için ayrı doğrulanmış bir binary dağıtımı değildir.

Önerilen dizilim; her yol hedef kullanıcıdan üretilir:

```
~/Applications/SatinAlmaAgent/releases/0.3.0/   # çıkarılan sürüm, değişmez kaynak
~/Library/Application Support/SatinAlmaAgent/
    config.json                             # sır içermez
    runtime/                                # izole Python ortamı
    langflow/                               # Langflow DB ve güvenli ayarları
    state/cases/requests.db                  # talepler, teklifler ve iş kayıtları
    state/cases/drafts/                      # raporlar ve taslaklar
    state/monitor/                          # kaynaklı araştırmalar
    staging/                                # hedefe hazırlanmış akış
    acceptance/                             # kurulum doğrulama kayıtları
```

`config.json.state_dir` mutlak yol olmalı, çıkarılan paketin tamamen dışında bulunmalı. `$HOME` ve `~` JSON içinde otomatik açılmaz. `database_path` bu pilotta `null` kalsın: worker ayarları Langflow API'sinden okur, hedef DB'yi doğrudan açmaz. Başka uygulamanın veritabanını göstermeyin.

Önce ZIP SHA-256'sını ayrı teslim kaydıyla karşılaştırın. Çıkardıktan sonra Python 3.12 ve uv mevcutsa paket kökünde:

```sh
uv run --no-project --python 3.12 python -B transfer/verify_package.py .
```

Bu doğrulayıcı kurulum veya kod çalıştırma yapmaz. Dosya listesi, boyut, hash, yol ve temiz akış kontrolleri yapar. İmzalı dağıtım/bağımsız yayıncı doğrulaması bu sürümde yoktur. Doğrulama sonrasında kaynakları değiştirmeyin. Testlerin ürettiği `__pycache__` dosyaları nedeniyle aynı klasörde sonraki tam envanter kontrolü için yeni, temiz bir çıkarım kullanın; iş verisi silmeyin.

## 2. İzole bağımlılıklar

Yalnız eksik olan uv/Python bileşenini resmi kaynaktan kurun. Paket içindeki `requirements.txt` aday bağımlılık listesidir. Langflow 1.12.4 PyPI sürümü mevcuttur; geliştirme checkout'u ile yayımlanmış wheel aynı kaynak sürümü olarak varsayılmaz. Bu kit tüm geçişli bağımlılıkları veya offline wheel deposunu içermez. İlk hedef kurulumunda çözümlenen sürümleri ayrı kabul kaydına alın; başarılı ortamı yeniden kurmak için hedefe özgü hash'li kilit kaydı üretin.

Mevcut başka sanal ortamı kullanmadan, hedefe özgü yollarla:

```sh
SATINALMA_BASE="$HOME/Library/Application Support/SatinAlmaAgent"
SATINALMA_RUNTIME="$SATINALMA_BASE/runtime"
uv venv --python 3.12 "$SATINALMA_RUNTIME"
uv pip install --python "$SATINALMA_RUNTIME/bin/python" -r requirements.txt
uv pip check --python "$SATINALMA_RUNTIME/bin/python"
```

Ortam zaten varsa üzerine kurulum yapmadan önce hangi kuruluma ait olduğunu doğrulayın. Aşağıdaki Python komutlarında açık yorumlayıcı kullanılmasının nedeni aynı ortamı korumaktır:

```sh
PYTHONDONTWRITEBYTECODE=1 uv run --no-project --no-sync --python "$SATINALMA_RUNTIME/bin/python" python transfer/run_offline_tests.py --suite root
PYTHONDONTWRITEBYTECODE=1 uv run --no-project --no-sync --python "$SATINALMA_RUNTIME/bin/python" python transfer/run_offline_tests.py --suite components
PYTHONDONTWRITEBYTECODE=1 uv run --no-project --no-sync --python "$SATINALMA_RUNTIME/bin/python" python -m pytest -p no:cacheprovider -q agent/components/test_claude_supplier_search.py
```

Testlerde `SATINALMA_CONFIG` ve `SATINALMA_STATE_DIR` tanımlı olmasın; testler sentetik geçici dosyalar kullanır ve ağ/model çağrılarını taklit eder. Gerçek kuyruk test için kullanılmaz. Başarısız testte bağımlılık veya kod uyumunu çözmeden ilerlemeyin.

Test yardımcısı, yalnız test sürecindeki importlar için geçici sentetik kaynak kökü; alt süreçler için de kullanılabilir flow/anahtar içermeyen dış TEMP config sağlar. Dağıtım dosyalarını ve MANIFEST'i değiştirmez. Bu işlem hedef bağlantı kabulü değildir. Gerçek paket worker'ları config eksikse geliştirme yoluna dönmeden hata verir; bu paket davranışı ayrıca olumsuz testle doğrulanır. Bağlantısız SAP şema aracı config istemez.

## 3. Hedef config ve akış hazırlama

`config.example.json` dosyasından dış dizinde yeni config oluşturun. `state_dir` değerini hedef kullanıcının gerçek mutlak yoluna çevirin. `langflow.flow_id` ilk aşamada `null` olmalı; base URL kullanılacak boş loopback portunu göstermeli. API anahtarı yerine yalnız değişken adı saklanır.

Paket kökünde:

```sh
mkdir -p "$SATINALMA_BASE/staging" "$SATINALMA_BASE/acceptance"
uv run --no-project --no-sync --python "$SATINALMA_RUNTIME/bin/python" python transfer/target_setup.py preflight --config "$SATINALMA_BASE/config.json"
uv run --no-project --no-sync --python "$SATINALMA_RUNTIME/bin/python" python transfer/target_setup.py prepare-flow --config "$SATINALMA_BASE/config.json" --source flow.json --output "$SATINALMA_BASE/staging/flow.target.json"
```

`preflight` servis/API/SAP/model bağlantısını sınamaz. Sır okumaz ve kuyruk oluşturmaz. `prepare-flow` yalnız yeni hedef akış dosyasını oluşturur; mevcut dosyayı ezmez. Talep deposu, RFQ raporları ve araştırma dizinini aynı dış `state_dir` altına bağlar. İş mantığını değiştirmez. Hazırlanan JSON hedef klasör yollarını içerdiğinden herkese açık repoya koymayın. Veri klasörü ileride taşınırsa bağlı akış yolları da kontrollü yeni sürümle güncellenmelidir.

## 4. Langflow'u yerel ve kimlik doğrulamalı açma

Önce seçili portun boş olduğunu doğrulayın. Başka servis çalışıyorsa kapatmayın. Aşağıdaki değişkenler yeni, ayrı Langflow örneğinin terminaline aittir:

```sh
export LANGFLOW_CONFIG_DIR="$SATINALMA_BASE/langflow"
export LANGFLOW_SAVE_DB_IN_CONFIG_DIR=true
export LANGFLOW_AUTO_LOGIN=false
export LANGFLOW_SKIP_AUTH_AUTO_LOGIN=false
```

Langflow bu kalıcı dizinde kendi imzalama/şifreleme anahtarlarını üretirse bunları da korunacak yedeğe dahil edin. Mevcut başka kurulumdan sır kopyalamayın; her başlatmada yeni anahtar üretmeyin. Operatörün güçlü parolasını **gizli terminal istemiyle** alın:

```sh
LANGFLOW_ENABLE_SUPERUSER_CLI=true uv run --no-project --no-sync --python "$SATINALMA_RUNTIME/bin/python" langflow superuser
uv run --no-project --no-sync --python "$SATINALMA_RUNTIME/bin/python" langflow run --host 127.0.0.1 --port 7860 --no-open-browser
```

CLI alt komutu veya ilk kullanıcı oluşturma davranışı yayımlanmış pakette farklıysa `langflow --help` ve resmi sürüm dokümanıyla çözün; auth'ı kapatarak geçmeyin. Parolayı `--password` argümanıyla veya sohbetle vermeyin. Başka port seçtiyseniz hem başlatma hem config URL'sini aynı yapın.

`/health` yalnız sürecin yanıt verdiğini gösterebilir; kurulu sürümde desteklenen `/health_check` veya `/healthz` hazır olma kontrolünü, `/api/v1/version` sonucunu ve oturum açmayı ayrıca doğrulayın. Anahtarsız `/api/v1/flows/` isteği reddedilmeli. UI'dan `staging/flow.target.json` dosyasını **yeni akış olarak** içe aktarın. 21 bileşen / 28 bağlantıyı ve boş girişleri doğrulayın; çıkan yeni UUID'yi `config.json` içine yazın. Eski geliştirme UUID'sini kullanmayın. `prepare-flow` tekrar import ederek kopya yaratmak için kullanılmaz.

## 5. Worker ve model ayarları

Langflow UI'da bu operatöre ait API anahtarı oluşturun. Kullanıcı anahtarı güvenli ortamdan `SATINALMA_LANGFLOW_API_KEY` değişkenine aktarsın; terminal çıktısında değer görünmesin. Sır içermeyen config yolu:

```sh
export SATINALMA_CONFIG="$SATINALMA_BASE/config.json"
uv run --no-project --no-sync --python "$SATINALMA_RUNTIME/bin/python" python agent/case_worker.py status
```

Hedef modda API anahtarı yoksa/akış erişilemezse auto-login, SQLite veya eski JSON'a geri dönüş yapılmaz. `flow_found=true` yalnız yapılandırmanın okunabildiğini gösterir; `mailbox_connectivity_verified` ve `sap_connectivity_verified` ayrı kanıt olmadan false kalır.

Model anahtarları kullanıcı tarafından Langflow'un güvenli değişkenlerinde `OPENAI_API_KEY` ve `ANTHROPIC_API_KEY` isimleriyle tanımlanır. Anahtarlar worker config'ine yazılmaz. Temiz akışta SAP API, canlı kur/Brent ve araştırma kaydı anahtarları kapalıdır. Önce seçilmiş sentetik talep ve kesin bitiş süresiyle küçük deneme yapılır. Kaydetme gerekiyorsa yalnız sentetik state için açıkça etkinleştirilir. OpenAI kotası ve Anthropic arama yetkisi hedefte ayrıca doğrulanır; birinin başarısı ortak çalışmayı kanıtlamaz.

## 6. Uzak noktalardan kullanım

İlk pilotta Mac mini ana bilgisayardır. Yetkili kullanıcılar şirket VPN'i ve BT'nin sağladığı SSH erişimi üzerinden tünel açabilir. Örnek, istemci bilgisayarda BT'nin verdiği kullanıcı/host ile:

```sh
ssh -N -L 127.0.0.1:17860:127.0.0.1:7860 KULLANICI@MAC_MINI_ADRESI
```

İstemci tarayıcısı `http://127.0.0.1:17860` adresini açar. Örnekteki adlar gerçek ayar değildir. Başlangıçta Mac mini'nin 7860 portunu internete açmayın. Her istemcide SSH ve Langflow kimliği ayrıca doğrulanır. Paylaşılan admin hesabı 1–5 kişilik yetki/onay panelinin yerine geçmez. Panel, roller, audit ve kurumsal HTTPS erişimi tamamlanmadan çok kullanıcılı canlı kabul vermeyin. Kaynakta RBAC bayrağını açmak tek başına gerçek yetki denetimi sağlamaz.

## 7. Kabul ve durdurma koşulları

Kabul kaydına ayrı ayrı yazın: paket hash'i; macOS/mimari/Python/kurulu paket sürümleri; geçen test sayıları; ayrı DB/klasörler; sağlık/sürüm/auth kontrolleri; yeni flow UUID'si; sentetik uçtan uca çıktı; gerçek iki-model iş kimliği/sonucu; yeniden başlatma sonrası durum; yedekten geçici geri dönüş sonucu. Sır, mail içeriği ve gerçek ticari veriyi bu kayda koymayın.

Kaynak bilgisayarda kişisel DEV oturumuyla standart PR servisinin HTTPS metadata ve seçilmiş tek kalem GET okuması doğrulandı. Bu sonuç Mac mini'nin bağımsız Python istemcisiyle TLS/kimlik/kodlama kabulü değildir; gerçek adres, sertifika ve iş verisi bu pakette bulunmaz. Örnek kalem kısmen siparişli olduğu için kuyruğa kabul edilmedi. Alan anlamları, 12 kaynak eşlemesi, hammadde/sarf ve organizasyon kapsamı ile talep sahibi eşlemesi halen açık; ayrı bot adresi de hazır değildir.

Görsel SAP bileşeni sınırlı public HTTPS/443 GET ve None/Bearer desteğini korur. Ayrı `agent/sap_worker.py` CLI istemcisi kurumsal hedef için hazırlanmıştır: dış config'te tek HTTPS origin/servis/entity seti, BT izinli IP ağları, geçerli TLS ve None/Basic/Bearer ortam değişkeni başvuruları. Görsel düğümün güvenlik kısıtlarını gevşetmeyin veya yeni adapteri ona kendiliğinden bağlı saymayın. Yeni `agent/sap_diagnostics.py` ile önce şema ve tek ham API anahtarı tanısı yapılabilir; tanı dosyası bir talep/kuyruk girdisi değildir. `sap/README.md` sırasını izleyin. İş sözleşmesi ayrıca tamamlanırsa worker yalnız yeni dış kaynak JSON dosyasına yazar; kuyruğa aktarım ayrı insan adımıdır. Gerçek PR alan anlamları/okuma yetkisi ve hedef bağlantısı ayrıca doğrulanmalı; test ve canlı sistem ayrılmalıdır. SSO/OAuth edinimi bu sürümde yoktur. Posta okuyucu/gönderici adapteri, onay ekranı ve bağımsız zamanlayıcı da ayrı kabul gerektirir.

SAP erişimi yoksa BT'nin sağladığı tam metadata dosyasını `agent/sap_schema_review.py --metadata "$SAP_METADATA_FILE" --output "$SAP_SCHEMA_REPORT"` ile yerelde inceleyin. Komut aynı izole Python ortamıyla çalıştırılır. İki yol da mutlak olmalı; rapor sürüm dışında yeni bir dosyadır. Endpoint/kimlik istemez, network/queue çağırmaz. Bu inceleme kurulum veya canlı bağlantı kabulünü ilerletmiş sayılmaz; eksik anahtar/tip/kişi eşlemeleri gerçek kaynak ve iş kuralıyla çözülür. Ayrıntılı komut ve çıktı sınırları `sap/README.md` içindedir.

İstenen takvim: talepler/teklifler her gün Türkiye saatiyle 10.00/16.00; hammadde haberleri pazartesi/cuma. Bu kit bu işleri zamanlamaz; mevcut bilgisayardaki Codex otomasyonu da taşınmaz. Aynı kuyruğu iki bilgisayardan eşzamanlı işlemeyin. Süre sonu raporu bir sonraki kontrol çalışınca üretilir; tam bitiş anı sözü verilmez.

## 8. Yedek, güncelleme ve geri dönüş

Önce worker ve bu kuruluma ait servisleri kontrollü durdurun; başka servisleri durdurmayın. Langflow'un kalıcı config/veritabanını, satın alma SQLite deposunu ve bütün bağlı rapor/araştırma dosyalarını tutarlı tek yedekte koruyun. SQLite WAL çalışırken yalnız `.db` dosyasını kopyalamayın; durdurulmuş temiz DB veya SQLite backup yöntemi kullanın. Yedekler ticari veri ve sır içerebilir; şirket erişim ve şifreleme kurallarıyla saklanır. Bu kaynak ZIP'i veri yedeği değildir.

Yeni sürümü farklı boş release klasörüne çıkarıp hash/test/uyumluluk kontrolü yapın. Mevcut state/config üzerine yeni örnek config kopyalamayın. Veri şeması geçişi gerekiyorsa önce geçici geri dönüş testinde doğrulayın. Eski kodu yeni şemayla rastgele çalıştırmayın. Yazılım geri dönüşünde korunacak iş kayıtlarını ayrıca belirleyin. Yedekten sonra gerçek veya sonucu belirsiz model çağrısı, harici gönderim ya da ticari işlem varsa eski iş DB'sini doğrudan geri yüklemeyin: güncel DB/attempt kayıtlarını koruyup dış işlem kimlikleriyle önce uzlaştırın. Attempt kaydını silmek aynı çağrının yeniden ücretlendirilmesine yol açabilir. İş verisi geri dönüşü ancak sonrasındaki olayların korunması ve şema uyumunun doğrulanmasıyla yapılır; gerçek kuyruğa test geri yüklemesi yapmayın.

İlk manuel kabul sonrası macOS açılışta servis, uyku/güç politikası, VPN geri bağlantısı, güvenli anahtar sağlama, log rotasyonu ve zamanlama için ayrı kurulum yapılır. Terminal açıkken çalışma, yeniden başlatmada sürekli hizmet kanıtı değildir.

Resmi kaynaklar: [Langflow kimlik doğrulama](https://docs.langflow.org/api-keys-and-authentication), [Langflow 1.12.4 dağıtımı](https://pypi.org/project/langflow/1.12.4/), [uv Python kurulumu](https://docs.astral.sh/uv/guides/install-python/).
