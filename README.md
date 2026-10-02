# Aqualung i770R HISTORY reset

A documented, unofficial Bluetooth method for resetting **HISTORY / TOTAL DIVE** on one Aqualung i770R. Dive-log deletion was **not achieved**. The supplied CLI is a refactoring of the successful prototype; it has automated contract tests but has not been rerun on hardware.

Aqualung i770RのHISTORYをBluetooth経由で0にした調査記録と、HISTORYだけを扱うPythonコードです。2026-10-02、バージョン応答`AQUA770R 2A 0006`の実機1台で、TOTAL DIVE **206 → 0**を確認しました。

**工場出荷時リセットではありません。ダイブログは残ります。** 累計時間・最大深度・最大潜水時間・深度合計も含むHISTORYの16バイトを0にします。別バージョン・別機種での書き込みは確認していません。

メーカー非公式の不揮発メモリ操作です。校正値と内部状態候補のバイト一致は確認しましたが、水中動作・減圧計算の正しさ・次の潜水時の採番は検証していません。この記録から潜水機器としての安全性は保証できません。

## 成功した変更

| 項目 | 実機で確認した結果 |
|---|---|
| 対象 | i770R / `AQUA770R 2A 0006` |
| 通信 | Mac / Bluetooth LE / Python・Bleak |
| 変更範囲 | `0x0060–0x006F`、16バイトページ番号6 |
| TOTAL DIVE | 206 → 0 |
| B2の応答 | `5A`（ACK） |
| 低位256バイトの差分 | HISTORY内の9バイトのみ |
| 校正候補 `0x0010–0x003F` | 完全一致 |
| 内部状態候補 `0x3FE00` / `0x3FF00` | 各256バイトが完全一致 |
| 別接続での読み直し | HISTORYの16バイトがすべて0 |
| 本体画面 | 所有者がHISTORYのリセットを確認 |

成功の要点は、`B2 + ページ番号 + 16バイト + 和`を**1つの論理コマンドとして送る**ことでした。ページ番号まで送って先にACKを待つ方式はNAKでした。詳細は[通信形式](docs/protocol.md)と[検証記録](docs/verification.md)を参照してください。

## コードの位置づけ

実機で成功した調査用試作コードは、他のローカルファイルを読み込む構成でした。公開版`i770r_history.py`は依存を整理した実装で、次の制限を追加しています。

- 機器名・メモリ内の識別情報・バージョンを照合する。
- 書き込み先はHISTORYページ6に固定する。
- バックアップを保存してから、同じ値の書き戻しと読み直しを行う。
- 同値試験に成功した場合だけ0を書き、対象外領域も読み直す。
- ACKなし・チェックサム不一致・対象外の変化で停止する。自動再送や自動復元はしない。

**公開版CLIの実機動作は未確認です。** 元の成功記録と、公開版の模擬GATT境界でのテストを区別してください。

## 実行方法

Python 3.11以上、Bluetooth対応PC、[uv](https://docs.astral.sh/uv/)を使用します。実機で成功したOSはApple SiliconのmacOSです。Windows・Linuxは未検証です。

```sh
git clone https://github.com/Utopi-a/aqualung-i770r-history-reset.git
cd aqualung-i770r-history-reset
uv sync --locked
uv run i770r-history --help
```

以下の`FQ123456`は例です。自分のi770Rが広告するBluetooth名に置き換えてください。

1. 本体を水面モードで起こし、BluetoothをONにする。
2. USBクリップを外し、DiverLog+など他の接続を終了する。
3. macOSがBluetoothへのアクセス許可を求めたら、使用するターミナルに許可する。
4. まず読み取りだけを実行する。

```sh
uv run i770r-history inspect --name FQ123456
```

HISTORYを0にするコマンドです。`--backup`には新しい保存先を指定します。

```sh
uv run i770r-history reset-history \
  --name FQ123456 \
  --backup backups/before-history-reset \
  --confirm-reset-history
```

成功時は`status: reset_verified`になります。同じ接続での読み直しまでを含みます。ACKだけでは成功と判定しません。既に全16バイトが0の場合は`already_zero`となり、メモリ書き込みを送りません。

切断後、実機画面と、もう一度`inspect`した結果を確認してください。Bluetoothは画面のスリープ中に停止します。応答が止まったときは自動的に再送せず、本体の状態と保存された`failure.json`を確認してください。

## バックアップと復元

バックアップには機器識別情報・校正値・内部状態候補が入ります。**GitHubにアップロードしないでください。** `backups/`とバイナリは`.gitignore`で除外しています。POSIX環境では保存先は0700、ファイルは0600で作ります。全メモリのバックアップではありません。

HISTORYだけを元に戻すには、同じ機器の保存先を使います。

```sh
uv run i770r-history restore-history \
  --name FQ123456 \
  --backup backups/before-history-reset \
  --confirm-restore-history
```

復元対象はバックアップ中のHISTORYの16バイトだけです。バックアップのSHA-256と機器を照合し、現在値が0または元の値で、対象外領域・内部状態候補が一致している場合に限ります。新しい潜水などで状態が変わっていれば復元を拒否します。校正や組織状態候補をバックアップから書き戻す機能はありません。

書き込みのACKが失われた場合は、結果が不明です。同じ復元の自動繰り返しも禁止しています。`restore-attempt.json`が既にある保存先では新たな復元送信をしません。

## ログと表示番号

HISTORYとダイブログは別の領域です。HISTORY=0と古いログが同時に存在します。

ログ本文をFFにした試行では、索引が残ったため選択枠も残りました。ログ本文は全606ページを元のバックアップへ復元し、完全一致を確認しました。索引ページへの変更は応答が得られず、ログ消去には至っていません。**ログ本文のFF化をリセット手順として公開していません。**

`8 → 7 → … → 1 → 99 → 98 …`という番号だけから順序破損とは断定できません。復元した索引では99件が新しい記録から1件ずつ遡り、重複なく元の記録と対応しました。現在の本体画面での日付順と、この番号の表示規則は未確認です。[ログ順序の確認範囲](docs/log-order.md)を参照してください。

## テスト

実機に接続せず、GATT境界を模擬して検証します。

```sh
uv run python -m unittest discover -s tests -v
uv run python -m compileall -q i770r_history.py tests
```

フレーム形式、和、同値試験、HISTORYだけの変更、バックアップ、復元、誤機器・誤バージョン・NAK・ACK欠落・読み直し不一致での停止を確認します。模擬試験は実機検証や水中試験の代わりにはなりません。

## 参考資料とライセンス

- [Aqualung i770R Owner's Manual](https://files.plytix.com/api/v1.1/file/public_files/pim/assets/0b/ab/e6/61/61e6ab0b426259d4c914e07f/texts/0c/8e/36/64/64368e0c02b279dd763d3b98/i770R_owners_manual_EN.pdf)：Bluetooth、HISTORY、LOGの説明。
- [libdivecomputer](https://github.com/libdivecomputer/libdivecomputer)、特に`src/oceanic_atom2.c`：読み取り・ページ番号・チェックサムを理解する参考。
- [Bleak](https://github.com/hbldh/bleak)：公開CLIのBLE通信ライブラリ。

調査では、所有者のAndroidから取得したDiverLog+の通信処理もローカルで確認しました。APK、逆コンパイルしたコード、メーカーのファームウェア、マニュアル本文はこのリポジトリに収録していません。掲載したコードはPythonでの通信実装です。ライセンスは[GPL-3.0-or-later](LICENSE)です。
