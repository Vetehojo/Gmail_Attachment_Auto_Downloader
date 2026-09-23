# 第三者ソフトウェアのライセンス

本ソフトウェア本体は MIT License で提供する（[LICENSE](LICENSE)）。
実行に必要なパッケージは、`setup.bat` が `requirements.lock` に記載した
固定バージョンで利用者の環境へ個別に導入する。本リポジトリはこれらのコードを同梱しない。
各パッケージにはそれぞれのライセンスが適用される。主なパッケージを次に示す
（導入される全パッケージとバージョンは `requirements.lock` を参照）。

| パッケージ | ライセンス | 配布元 |
| --- | --- | --- |
| google-api-python-client | Apache License 2.0 | https://github.com/googleapis/google-api-python-client |
| google-auth | Apache License 2.0 | https://github.com/googleapis/google-auth-library-python |
| google-auth-httplib2 | Apache License 2.0 | https://github.com/googleapis/google-auth-library-python-httplib2 |
| google-auth-oauthlib | Apache License 2.0 | https://github.com/googleapis/google-auth-library-python-oauthlib |
| httplib2（google-auth-httplib2 の依存） | MIT License | https://github.com/httplib2/httplib2 |
| six（pystray の依存） | MIT License | https://github.com/benjaminp/six |
| certifi（requests の依存） | Mozilla Public License 2.0 | https://github.com/certifi/python-certifi |
| Pillow | MIT-CMU License | https://github.com/python-pillow/Pillow |
| **pystray** | **GNU Lesser General Public License v3.0** | https://github.com/moses-palmer/pystray |

## pystray（LGPL-3.0）についての注意

pystray はタスクトレイの表示にのみ使用しており、利用者の環境に導入された
ライブラリをインポートして呼び出しているだけである。LGPL-3.0 は、ライブラリを
改変せずに利用するアプリケーション側へ同一ライセンスでの公開を要求しないため、
本ソフトウェアを MIT License で配布することに支障はない。利用者は自分の環境で
pystray を任意のバージョンへ差し替えられる。

**ただし、本ソフトウェアを PyInstaller などで pystray ごと単一の実行ファイルに
固めて再配布する場合は事情が変わる。** その形態では LGPL-3.0 第 4 条が求める
「利用者がライブラリを差し替えられる状態」を別途担保する必要がある。
そのような配布を行う場合は改めてライセンス条件を確認すること。
本リポジトリはソースコードでの配布のみを想定している。

## Google API の利用について

本ソフトウェアは Google の API を呼び出すが、Google が提供・保証するものではなく、
Google とは何の関係もない。API の利用には Google の利用規約が適用される。
Gmail、Google Workspace、Google Cloud は Google LLC の商標である。
