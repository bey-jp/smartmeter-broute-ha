# AI 指示書：スマートメーター Bルートを Home Assistant につなぐ

この文書は、Claude Code などの AI エージェントが読んで、そのまま作業するための手順書です。
人が読んでも分かるように書いてあります。

## 前提をユーザーに確認する

始める前に、次をユーザーに聞いてください。分からないものがあれば、そこで止めて説明します。

1. Bルートの **認証 ID（32桁）とパスワード（12桁）** が手元にあるか。まだなら、電力会社（一般送配電事業者）の「電力メーター情報発信サービス」に申し込む必要がある（無料、届くまで1〜2週間ほど）。
2. Wi-SUN ドングル（**RL7023 Stick-D/IPS** など SKSTACK IP 対応のもの）を、どの Linux 機（Raspberry Pi など）に挿したか。
3. Home Assistant に **Mosquitto broker** アドオンと **MQTT 統合** が入っているか。
4. その Linux 機に SSH などでコマンドを打てるか。root（sudo）が使えるか。

**ID とパスワードは秘密の情報です。** 画面やログ、チャットに出さないでください。設定ファイルに書くときも、値を表示せずに書き込みます。

## 手順

### 1. ドングルを確認する

```sh
ls /dev/serial/by-id/
```

`usb-FTDI_FT230X_Basic_UART_…-if00-port0` のような名前があれば認識しています。無ければ、挿し直す・別のポートに挿すようユーザーに頼みます。

### 2. ファイルと Python のライブラリを用意する

```sh
git clone https://github.com/bey-jp/smartmeter-broute-ha.git ~/smartmeter-broute-ha
python3 -c "import serial, paho.mqtt"   # エラーなら下を実行
sudo apt install python3-serial python3-paho-mqtt
id -nG | grep -w dialout || sudo usermod -aG dialout $USER   # 追加したらログインし直す
```

### 3. 設定ファイルを作る

```sh
mkdir -p ~/.config/smartmeter
cp ~/smartmeter-broute-ha/config.ini.example ~/.config/smartmeter/config.ini
chmod 600 ~/.config/smartmeter/config.ini
```

`[device] serial` に、手順1で見えた by-id のフルパスを書きます。

ドングルが応答するか確かめます。

```sh
python3 ~/smartmeter-broute-ha/smartmeter.py --check
```

`RL7023: EVER 1.2.8` のように出れば OK です。

### 4. 電波を確かめる（ID なしでできる）

近所のメーターが見えるかを、認証なしのスキャンで確かめます。

```python
import serial, time
s = serial.Serial("<by-id のパス>", 115200, timeout=2)
s.write(b"SKSCAN 3 FFFFFFFF 6\r\n")
end = time.time() + 60
while time.time() < end:
    l = s.readline().decode(errors="replace").strip()
    if l: print(l)
    if l.startswith("EVENT 22"): break
```

`EPANDESC` の後に `Channel` `Pan ID` `Addr` `LQI` が出れば、電波は届いています。1つも出なければ、ドングルの場所を変える（USB 延長ケーブルで窓やメーターの方向へ）ようユーザーに伝えます。

### 5. Mosquitto に専用のログインを作る

HA の 設定 → アドオン → Mosquitto broker → 構成 の `logins` に、ユーザー `smartmeter` とランダムなパスワードを追加して保存し、アドオンを再起動します。
**既存のログインを消さないこと**（ほかの機器が使っています）。同じユーザー名とパスワードを `config.ini` の `[mqtt]` に書きます。HA と同じ機械なら host は `127.0.0.1`、別の機械なら HA の IP アドレスです。

### 6. ID とパスワードを書く

`config.ini` の `[broute] id` と `password` に書きます。ユーザーが通知書のファイル（パスワード付き Excel など）しか持っていない場合は、ファイルを開くパスワードを聞いて、値を表示せずに読み出して書き込みます。

### 7. 常駐させる

`systemd/smartmeter.service` の `User` とパス（`/home/pi/smartmeter-broute-ha`）を、実際のユーザー名と置き場所に書き換えてから入れます。

```sh
sudo cp ~/smartmeter-broute-ha/systemd/smartmeter.service /etc/systemd/system/
systemctl is-active ModemManager && sudo cp ~/smartmeter-broute-ha/systemd/99-rl7023-mm-ignore.rules /etc/udev/rules.d/ && sudo udevadm control --reload
sudo systemctl daemon-reload
sudo systemctl enable --now smartmeter
journalctl -u smartmeter -f
```

`メーター接続: ON` と `積算電力量 係数 …` が出れば成功です。HA に `sensor.smartmeter_power` などができ、値が15秒ごとに変わることを確かめます。

### 8. エネルギーダッシュボード

HA の 設定 → ダッシュボード → エネルギー → 「電力網からの消費」に `sensor.smartmeter_energy` を登録するよう、ユーザーに案内します（AI が WebSocket の `energy/save_prefs` で登録してもよい。その場合は先に `energy/get_prefs` で今の設定を読み、ほかの設定を消さないこと）。

## うまくいかないとき

- `メーターが見つからない`：ID の打ち間違い、Bルートがまだ開通していない、電波が弱い。手順4のスキャンで、近所のメーターが見えるかを確かめる。
- `PANA 認証に失敗`：パスワードの打ち間違い（0 と O、1 と I）。
- `応答なし` が続く：電波が弱い。ドングルの場所を変える。
- `シリアルのエラー`：ポート名の違い、dialout に入っていない、ほかのプログラム（`--check` など）が同時に使っている。サービスを止めてから試す。
