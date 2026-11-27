# スマートメーター Bルート × Home Assistant

電力会社のスマートメーターから **Bルート**（Wi-SUN）で電力を読み取り、**Home Assistant（HA）** のセンサーにする Python スクリプトです。Wi-SUN ドングル **RL7023 Stick-D/IPS**（テセラ・テクノロジー）を Raspberry Pi に挿して使います。

Read your Japanese smart meter over the B-route (Wi-SUN, ECHONET Lite) with an RL7023 Stick-D/IPS dongle and publish the values to Home Assistant via MQTT Discovery. Robust reconnect handling; runs as a systemd service. Japanese docs; see `AI_INSTRUCTIONS.md` to let an AI agent set it up for you.

- 瞬時電力（W、15秒ごと）・瞬時電流（A）・積算電力量（kWh）を HA に送る
- HA の **エネルギーダッシュボード** にそのまま登録できる（1時間・1日・1か月ごとの使用量）
- MQTT Discovery なので、HA 側の YAML 設定は不要（デバイス「スマートメーター」が自動でできる）
- 切れても自動でつなぎ直す（再参加 → 再スキャン → ドングルのリセットの順）
- ID とパスワードは設定ファイルに分け、スクリプトには書かない

筆者は **Raspberry Pi 5（Home Assistant Supervised）＋ RL7023 Stick-D/IPS（ファーム EVER 1.2.8）**、九州電力エリアで使っています。SKSTACK IP を使うほかのドングル（ROHM BP35A1 / BP35C2 など）でも、ほぼそのまま動くはずです（未確認）。

> **AI にやってもらう**：Claude Code などに次の1行を送ると、手順どおりに設定してくれます。
>
> ```
> https://github.com/bey-jp/smartmeter-broute-ha/blob/main/AI_INSTRUCTIONS.md を読んで、その手順どおりに、私のスマートメーターを Home Assistant につないでください。
> ```

## 仕組み

```
[スマートメーター] ─Wi-SUN 920MHz─ [RL7023] ─USB─ [Raspberry Pi]
                                                   smartmeter.py（systemd で常駐）
                                                     │ MQTT
                                                     ▼
                                          [Mosquitto] → [Home Assistant]
                                                         ├ sensor.smartmeter_power   瞬時電力 W
                                                         ├ sensor.smartmeter_energy  積算電力量 kWh
                                                         └ エネルギーダッシュボード
```

1. RL7023 に Bルートのパスワードと ID を設定し、`SKSCAN 2`（ペアリング ID 付きスキャン）で自宅のメーターを探す
2. `SKJOIN` で PANA 認証してメーターとつなぐ
3. ECHONET Lite の Get で、低圧スマート電力量メータ（0x028801）から E7（瞬時電力）・E8（瞬時電流）・E0（積算電力量）を読む
4. 読んだ値を MQTT で送る。HA は MQTT Discovery でセンサーを作り、値は HA の DB（記録）にたまる

InfluxDB などの別のデータベースは使いません。HA の記録（recorder）と長期統計にたまります。

## 取れたり取れなかったりしないための工夫

ネットでよく見る Bルートのサンプルをそのまま使うと、「データが取れるときと取れないときがある」「しばらくすると止まる」ことがよくあります。筆者が昔使っていたスクリプトもそうでした。このスクリプトでは次のようにしています。

| よくある原因 | このスクリプト |
|---|---|
| `SKSENDTO` の後に1行だけ読み、コマンドのエコーや `EVENT 21` を応答と取り違える | エコーを切り（`SKSREG SFE 0`）、締め切りまで `ERXUDP` を待ち、ECHONET Lite の TID で自分の要求への応答か照合する |
| 切断（`EVENT 26/27/28`）や再認証の失敗（`EVENT 24`）に気づかず止まる | イベントを常に見張り、切れたら `SKTERM` → 再参加。応答なしが4回続いても再参加 |
| 毎回スキャンして時間がかかる・失敗する | 見つけたメーターを覚えておき、次回はスキャンせずに参加。失敗したらスキャンし直す |
| ARIB の送信時間制限（`EVENT 32`）中も送り続ける | 解除（`EVENT 33`）まで送らない |
| E8（電流）の単位を 0.01A と誤る | 正しい 0.1A で計算。単相2線などで値が無い相（0x7FFE）は送らない |
| 積算電力量の係数・単位を決め打ち | D3（係数）と E1（単位）をメーターから読む |
| `ERXUDP` の列の数がファームで違う（SIDE 列の有無） | 後ろから数えて読む |

## 必要なもの

- **Bルートの認証 ID（32桁）とパスワード（12桁）**：電力会社（一般送配電事業者）に「電力メーター情報発信サービス（Bルートサービス）」を申し込むと届きます（無料）。メーターが Bルート対応でなければ交換してもらえます。
- **Wi-SUN ドングル RL7023 Stick-D/IPS**（または SKSTACK IP 対応のドングル）
- **Linux の機械**（Raspberry Pi など）：Python 3、`pyserial`、`paho-mqtt`（1.6 系で確認。2.x でも動くはず）
- **Home Assistant** と **Mosquitto broker** アドオン（MQTT 統合が設定済み）

メーターまでの電波が弱いと、つながらなかったり切れやすくなったりします。マンションでは、置き場所を変えるだけで見えるメーターの数が大きく変わることがありました（同じ家の中で、ある場所では3台、別の場所では20台以上）。

## 入れ方

### 1. ファイルを置く

```sh
git clone https://github.com/bey-jp/smartmeter-broute-ha.git ~/smartmeter-broute-ha
sudo apt install python3-serial python3-paho-mqtt   # 入っていなければ
sudo usermod -aG dialout $USER                      # シリアルを使うため（入れたらログインし直す）
```

### 2. ドングルが応答するか確かめる

```sh
ls /dev/serial/by-id/          # usb-FTDI_FT230X_Basic_UART_…-if00-port0 があるか
mkdir -p ~/.config/smartmeter
cp ~/smartmeter-broute-ha/config.ini.example ~/.config/smartmeter/config.ini
chmod 600 ~/.config/smartmeter/config.ini
# config.ini の [device] serial に、上で見えた by-id のパスを書く
python3 ~/smartmeter-broute-ha/smartmeter.py --check    # → RL7023: EVER 1.2.8 など
```

### 3. Mosquitto に専用のログインを作る

HA の 設定 → アドオン → Mosquitto broker → 構成 → `logins` に追加します。

```yaml
- username: smartmeter
  password: （長いランダムな文字列）
```

保存してアドオンを再起動し、同じユーザー名とパスワードを `config.ini` の `[mqtt]` に書きます。

### 4. ID とパスワードを書く

`config.ini` の `[broute]` に、届いた認証 ID とパスワードを書きます。

```ini
[broute]
id = 0000000000000000000000000000ABCD
password = ABCDEFGHIJKL
```

> 九州電力送配電の場合、通知書はパスワード付きの Excel（.xls）でメールで届きました。メールの本文によると、個人のお客さまは、添付の開封パスワードが「a＋連絡先の電話番号」、Excel のパスワードが「供給地点特定番号の7〜10桁目（4桁）」です。暗号化された .xls は Google スプレッドシートでは開けないので、Excel か LibreOffice を使います。

### 5. 常駐させる

```sh
# systemd/smartmeter.service の User とパスを自分の環境に合わせてから
sudo cp ~/smartmeter-broute-ha/systemd/smartmeter.service /etc/systemd/system/
sudo cp ~/smartmeter-broute-ha/systemd/99-rl7023-mm-ignore.rules /etc/udev/rules.d/   # ModemManager がある場合
sudo udevadm control --reload
sudo systemctl daemon-reload
sudo systemctl enable --now smartmeter
journalctl -u smartmeter -f
```

うまくいくと、次のようなログが出ます。

```
INFO MQTT 接続
INFO スキャン（duration 6）
INFO メーター発見 Channel 3B Pan XXXX Addr XXXXXXXXXXXXXXXX LQI 27
INFO メーターに接続 FE80:0000:0000:0000:XXXX:XXXX:XXXX:XXXX
INFO メーター接続: ON
INFO 積算電力量 係数 1、単位 0.001 kWh
```

### 6. エネルギーダッシュボードに登録する

HA の 設定 → ダッシュボード → エネルギー → 「電力網からの消費」に `sensor.smartmeter_energy`（スマートメーター 積算電力量）を選びます。電力の欄には `sensor.smartmeter_power` も選べます。グラフは1時間たつと出始めます。

## HA にできるもの

| エンティティ | 内容 |
|---|---|
| `sensor.smartmeter_power` | 瞬時電力（W）。15秒ごと。3分更新が無いと「利用不可」 |
| `sensor.smartmeter_current_r` / `sensor.smartmeter_current_t` | 瞬時電流 R相・T相（A） |
| `sensor.smartmeter_energy` | 積算電力量（kWh、`total_increasing`）。60秒ごと |
| `binary_sensor.smartmeter_link` | メーターとつながっているか |

MQTT のトピックは `smartmeter/power`、`smartmeter/energy` など。`smartmeter/status`（online / offline）が LWT です。

## 設定（config.ini）

| 項目 | 内容 |
|---|---|
| `[broute] id / password` | Bルートの認証 ID・パスワード。空のあいだは待機し、10分ごとに読み直す |
| `[mqtt] host / port / username / password` | Mosquitto |
| `[device] serial` | RL7023 のシリアルポート（`/dev/serial/by-id/…` 推奨） |
| `[device] interval` | 瞬時電力・電流を読む間隔（秒、既定 15） |
| `[device] energy_interval` | 積算電力量を読む間隔（秒、既定 60） |

見つけたメーターの情報は `~/.local/state/smartmeter.json` に保存されます。メーターを交換したときなどは、このファイルを消すとスキャンし直します。

## うまくいかないとき

| ログ | 考えられること |
|---|---|
| `メーターが見つからない` | ID が違う／まだ Bルートが開通していない／電波が届いていない。ID 無しのスキャン（`SKSCAN 3`）で近所のメーターが見えるなら電波は来ている |
| `PANA 認証に失敗` | パスワードが違う（0 と O、1 と I の取り違えに注意） |
| `応答なし` が続く | 電波が弱い。ドングルを USB 延長ケーブルで窓やメーターの方向へ |
| `シリアルのエラー` | ポートの名前が違う／ほかのプログラムが使っている／dialout に入っていない |

## ライセンス

MIT License。ブログ記事：[bey.jp](https://bey.jp/)
