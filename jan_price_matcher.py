import os
import json
import re
import unicodedata
import gspread
from google.oauth2.service_account import Credentials
import numpy as np
import pandas as pd
from rapidfuzz import fuzz, process

# スプレッドシートIDの取得
MASTER_ID = os.environ.get("MASTER_SHEET_ID")
PRICE_ID = os.environ.get("PRICE_SHEET_ID")

# しきい値（環境変数で上書き可能）
MATCH_THRESHOLD = float(os.environ.get("MATCH_THRESHOLD", 95))
REVIEW_THRESHOLD = float(os.environ.get("REVIEW_THRESHOLD", 85))

OUTPUT_SHEET_NAME = '突合結果_自動出力'
# 手動指定の保存先と、プルダウンの選択肢に使う価格表の商品名リスト
MANUAL_SHEET_NAME = '手動対応表'
LIST_SHEET_NAME = '価格表_商品名リスト'
MANUAL_COL = '手動指定'
NO_MATCH = '(一致させない)'
# 結果の列名：一致が確定した価格表側の商品名と、要確認の候補
MATCHED_COL = '価格表側の商品名'
CANDIDATE_COL = '要確認_候補'

# 価格比較タブ：突合で商品名の対応が確定した商品について、各店舗の買取価格を並べる
COMPARE_SHEET_NAME = '価格比較'
MAIN_LABEL = 'ドラゴンボーン'  # メインの価格表（PRICE_ID）の店舗名

# 追加の買取価格表（店舗ごとに価格列を横に並べる）
# (列名の接頭辞, スプレッドシートIDの環境変数名, 読むタブ名)。IDが未設定の店舗は飛ばす。
# figure-kaitori-bot の形式（「現在一覧」タブ、状態=削除 の行は対象外）が基本。
EXTRA_PRICE_SOURCES = [
    ('千葉鑑定団', 'CHIBAKAN_PRICE_ID', '現在一覧'),
    ('らしんばん', 'LASHINBANG_PRICE_ID', '現在一覧'),
    # twitter-rss-discord-bot の承認ダッシュボードが書く形式（ジャンル/商品名/価格/更新日/型番・JAN）。
    # ダッシュボードは「sheet1」タブに書く（最初のタブ「シート1」とは別に作られる）
    ('ムーラン', 'MOULIN_PRICE_ID', 'sheet1'),
]
EXTRA_PRICE_TAB = '現在一覧'


def extra_names(label):
    """追加店舗ごとの 手動指定の列名・手動対応表のタブ名・プルダウン選択肢のタブ名"""
    return f'{label}_{MANUAL_COL}', f'{MANUAL_SHEET_NAME}_{label}', f'{LIST_SHEET_NAME}_{label}'

EXTRA_STATUS_COL = '状態'
EXTRA_REMOVED = '削除'
UPDATED_COL = '更新日'
WORK_COL = '作品名'

# タブの右側に残しておく空き列の数
SPARE_COLS = 5

# あいまい一致を一度に計算する行数（メモリ使用量の上限を抑えるため）
FUZZY_CHUNK_SIZE = 1000


def get_client():
    # GitHub Secretsの環境変数からサービスアカウントのJSON文字列を取得
    sa_key_json = os.environ.get("GCP_SA_KEY")
    if not sa_key_json:
        raise ValueError("環境変数 GCP_SA_KEY が設定されていません。")

    # プレーンテキストのJSON文字列から認証情報を直接構築
    sa_info = json.loads(sa_key_json)
    scopes = [
        'https://www.googleapis.com/auth/spreadsheets',
        'https://www.googleapis.com/auth/drive'
    ]
    creds = Credentials.from_service_account_info(sa_info, scopes=scopes)
    return gspread.authorize(creds)


# 店舗によって付いたり付かなかったりする、商品の区別に関係ない語（単語として独立しているときだけ取り除く）
NOISE_WORDS = {'フィギュア', 'figure', 'プライズ', 'prize', 'プライズフィギュア', '景品', '新作', '新弾', '新商品', 'new'}

# 要確認の候補を自動で確定させる条件（相互最良一致）
# ・類似度がこの値以上
# ・2番目に近い候補との差がこの値以上（紛らわしい候補がない）
AUTO_REVIEW_MIN = float(os.environ.get("AUTO_REVIEW_MIN", 90))
AUTO_REVIEW_MARGIN = float(os.environ.get("AUTO_REVIEW_MARGIN", 5))


def to_tokens(text):
    """商品名を単語に分ける。NFKCで全角半角・半角カナ・丸数字などを揃え、記号は区切りとして扱う"""
    if pd.isna(text) or not isinstance(text, str):
        return []
    text = unicodedata.normalize('NFKC', text).lower()
    # ひらがなはカタカナにそろえる（「ぬーどるストッパー」と「ヌードルストッパー」など）
    text = ''.join(chr(ord(ch) + 0x60) if 'ぁ' <= ch <= 'ゖ' else ch for ch in text)
    # 文字・数字・長音以外（記号・括弧・空白）はすべて区切りにする
    text = ''.join(ch if (unicodedata.category(ch)[0] in 'LN' or ch == 'ー') else ' ' for ch in text)
    return [t for t in text.split() if t not in NOISE_WORDS]


def clean_text(text):
    """商品名の比較精度を上げるための正規化処理（記号・空白を除いて詰めた文字列）"""
    return ''.join(to_tokens(text)).replace('ー', '')


def sorted_tokens(text):
    """語順の違いを吸収するため、単語を並べ替えてつないだ文字列"""
    return ' '.join(sorted(t.replace('ー', '') for t in to_tokens(text)))


# 一番くじなど「くじ」の景品。通常のプライズとは別商品なので、片方だけがくじなら一致させない
KUJI_PATTERN = re.compile(r'(?:くじ|クジ)(?![らラ])|一番賞|ラストワン|ダブルチャンス|(?<![a-z0-9])[a-z]賞')


def is_kuji(text):
    if pd.isna(text) or not isinstance(text, str):
        return False
    return bool(KUJI_PATTERN.search(unicodedata.normalize('NFKC', text).lower()))


def numbers_in(text):
    """商品名に含まれる数字（Vol.2・第3弾・A賞②など）。数字だけが違う別商品の取り違えを防ぐのに使う"""
    if pd.isna(text) or not isinstance(text, str):
        return ()
    return tuple(sorted(int(n) for n in re.findall(r'\d+', unicodedata.normalize('NFKC', text))))


def find_columns(df_master, df_price):
    """JAN列・マスターの商品名列・価格表の商品名列を列名から推定する"""
    jan_col = next((c for c in df_master.columns if 'JAN' in c or 'バーコード' in c), None)
    master_name_col = next((c for c in df_master.columns if '商品名' in c), df_master.columns[1] if len(df_master.columns) > 1 else df_master.columns[0])
    price_name_col = next((c for c in df_price.columns if '商品名' in c), df_price.columns[1] if len(df_price.columns) > 1 else df_price.columns[0])
    return jan_col, master_name_col, price_name_col


def make_key(jan, name):
    """手動指定のキー：JANがあればJAN、なければ商品名"""
    jan = '' if jan is None or pd.isna(jan) else str(jan).strip()
    return jan or str(name).strip()


def match_dataframes(df_master, df_price, threshold=MATCH_THRESHOLD, review_threshold=REVIEW_THRESHOLD, manual=None):
    """マスターの各商品が、価格表ではどの商品名で載っているかを突合し、結果のDataFrameを返す
    （manual: キー→価格表側の商品名 の手動指定）"""
    jan_col, master_name_col, price_name_col = find_columns(df_master, df_price)

    merged_df = df_master.copy()
    merged_df[MATCHED_COL] = ''
    merged_df['一致方法'] = ''
    merged_df['類似度'] = ''
    merged_df[CANDIDATE_COL] = ''
    keys = pd.Series([make_key(j, n) for j, n in zip(merged_df[jan_col] if jan_col else [None] * len(merged_df), merged_df[master_name_col])], index=merged_df.index)
    merged_df[MANUAL_COL] = keys.map(manual or {}).fillna('')

    master_names = merged_df[master_name_col].astype(str).str.strip()
    master_cleaned = master_names.map(clean_text)
    price_names = df_price[price_name_col].astype(str).str.strip()
    price_cleaned = price_names.map(clean_text)
    price_name_set = set(price_names[price_names != ''])

    # 突合が済んだ行（手動指定した行を含む）。以降の段階の対象から外す
    done = pd.Series(False, index=merged_df.index)

    # 0. 手動指定（自動突合より優先する）
    manual_vals = merged_df[MANUAL_COL].astype(str).str.strip()
    locked = manual_vals != ''
    if locked.any():
        print(f"手動指定 {locked.sum()} 件を反映しています...")
        no = manual_vals == NO_MATCH
        merged_df.loc[no, '一致方法'] = '手動: 一致させない'
        found = locked & ~no & manual_vals.isin(price_name_set)
        merged_df.loc[found, '一致方法'] = '手動指定'
        merged_df.loc[found, MATCHED_COL] = manual_vals[found]
        missing = locked & ~no & ~found
        merged_df.loc[missing, '一致方法'] = '手動指定 (価格表に見つかりません)'
        if missing.any():
            print(f"  価格表に見つからない手動指定: {missing.sum()}件")
        done |= locked

    # 1. 完全一致
    print("商品名による完全一致処理を実行中...")
    hit = ~done & (master_names != '') & master_names.isin(price_name_set)
    merged_df.loc[hit, '一致方法'] = '商品名による完全一致'
    merged_df.loc[hit, MATCHED_COL] = master_names[hit]
    done |= hit

    # 2. 正規化一致
    if (~done).any():
        print("表記ゆれ（スペース・全角半角）を吸収した完全一致処理を実行中...")
        valid = price_cleaned != ''
        cleaned_price_dict = dict(zip(price_cleaned[valid], price_names[valid]))
        hit = ~done & (master_cleaned != '') & master_cleaned.isin(cleaned_price_dict.keys())
        merged_df.loc[hit, '一致方法'] = '正規化による完全一致'
        merged_df.loc[hit, MATCHED_COL] = master_cleaned[hit].map(cleaned_price_dict)
        done |= hit

    # 2b. 語順ちがいの完全一致（店舗によって「キャラ名 作品名」「作品名 キャラ名」の順が違うため）
    if (~done).any():
        master_sorted = master_names.map(sorted_tokens)
        price_sorted = price_names.map(sorted_tokens)
        valid = price_sorted != ''
        sorted_price_dict = dict(zip(price_sorted[valid], price_names[valid]))
        hit = ~done & (master_sorted != '') & master_sorted.isin(sorted_price_dict.keys())
        merged_df.loc[hit, '一致方法'] = '語順ちがいの完全一致'
        merged_df.loc[hit, MATCHED_COL] = master_sorted[hit].map(sorted_price_dict)
        done |= hit

    # 3. 厳格あいまい一致（全CPUでまとめて計算）
    targets = merged_df.index[~done & (master_cleaned != '')]
    if len(targets) > 0 and (price_cleaned != '').any():
        print(f"残りの {len(targets)} 件について、しきい値 {threshold:g}% 以上（{review_threshold:g}% 以上は要確認）の厳格なあいまい検索を実行します...")
        # 同じ商品が何行も載っている価格表（記録形式など）で2番目の候補が同点にならないよう、正規化後の名前で重複を除く
        uniq = price_cleaned[price_cleaned != ''].drop_duplicates(keep='last')
        u_names = price_names[uniq.index]
        choices = uniq.tolist()
        choices_sorted = u_names.map(sorted_tokens).tolist()
        master_sorted_all = master_names.map(sorted_tokens)
        best_idx = np.empty(len(targets), dtype=np.int64)
        best_score = np.empty(len(targets), dtype=np.float64)
        second_score = np.zeros(len(targets), dtype=np.float64)
        for start in range(0, len(targets), FUZZY_CHUNK_SIZE):
            rows = targets[start:start + FUZZY_CHUNK_SIZE]
            scores = combined_scores(master_cleaned[rows].tolist(), master_sorted_all[rows].tolist(), choices, choices_sorted)
            n = len(rows)
            best_idx[start:start + n] = scores.argmax(axis=1)
            best_score[start:start + n] = scores.max(axis=1)
            if scores.shape[1] > 1:
                second_score[start:start + n] = np.partition(scores, -2, axis=1)[:, -2]
            print(f"  {min(start + FUZZY_CHUNK_SIZE, len(targets))}/{len(targets)} 件完了")

        best_score = best_score.round(1)
        cand_names = u_names.to_numpy()[best_idx]
        score_text = np.array([f'{s:g}' for s in best_score], dtype=object)

        # 数字（Vol.・第○弾・○賞など）が食い違う候補は、文字が似ていても別商品として自動では確定させない
        master_nums = [numbers_in(n) for n in master_names[targets]]
        cand_nums = [numbers_in(n) for n in cand_names]
        nums_same = np.array([a == b for a, b in zip(master_nums, cand_nums)], dtype=bool)
        # 両方に数字があって食い違う（第1弾と第2弾など）なら、別商品と判断して要確認にも出さない
        # 片方にしか数字がない場合は判断できないので要確認に残す
        nums_conflict = np.array([bool(a) and bool(b) and a != b for a, b in zip(master_nums, cand_nums)], dtype=bool)
        # 片方だけが一番くじなどの景品なら、名前が似ていても別商品
        kind_conflict = np.array([is_kuji(a) != is_kuji(b) for a, b in zip(master_names[targets], cand_names)], dtype=bool)
        nums_conflict &= ~kind_conflict

        merged_df.loc[targets, '類似度'] = score_text
        merged_df.loc[targets, '一致方法'] = [f'一致なし (最高類似度: {s}%)' for s in score_text]

        blocked = nums_conflict | kind_conflict
        ok = (best_score >= threshold) & nums_same & ~blocked
        demoted = (best_score >= threshold) & ~nums_same & ~blocked
        review = ~ok & ~blocked & (best_score >= review_threshold)
        conflict = nums_conflict & (best_score >= review_threshold)
        kuji = kind_conflict & (best_score >= review_threshold)

        # 要確認のうち、紛らわしさのないものは自動で確定する（相互最良一致）
        #   - 数字が一致し、類似度が AUTO_REVIEW_MIN 以上
        #   - 2番目の候補より AUTO_REVIEW_MARGIN 以上高い
        #   - 価格表側から見ても、マスターでいちばん近いのがこの商品（まだ載っていない新商品が、旧作に誤って結び付くのを防ぐ）
        # 価格表側から見て、マスターでいちばん近い商品（相互最良の判定に使う）
        mutual = np.zeros(len(targets), dtype=bool)
        if review.any():
            cand_pos = np.unique(best_idx[review])
            back = combined_scores(uniq.iloc[cand_pos].tolist(), u_names.iloc[cand_pos].map(sorted_tokens).tolist(),
                                   master_cleaned.tolist(), master_sorted_all.tolist())
            back_best = dict(zip(cand_pos, merged_df.index.to_numpy()[back.argmax(axis=1)]))
            mutual = np.array([back_best.get(p) == t for p, t in zip(best_idx, targets)], dtype=bool)

        auto = review & nums_same & mutual & (best_score >= AUTO_REVIEW_MIN) & (best_score - second_score >= AUTO_REVIEW_MARGIN)
        review &= ~auto

        # 価格表の1商品には、マスターの1商品だけを結び付ける
        #   - 候補がすでに別のマスター商品と一致済みなら、要確認に出さない
        #   - 候補から見てもっと近いマスター商品が他にあるなら、要確認はそちらだけに出す
        matched_now = pd.concat([merged_df.loc[done, MATCHED_COL], pd.Series(cand_names[ok | auto])]).astype(str)
        claimed = {clean_text(n) for n in matched_now if n.strip()}
        taken = review & np.array([clean_text(c) in claimed for c in cand_names], dtype=bool)
        not_closest = review & ~taken & ~mutual
        review &= ~taken & ~not_closest
        merged_df.loc[targets[taken], '一致方法'] = [
            f'一致なし (候補は別の商品と一致済み: {c} / 類似度: {s}%)' for c, s in zip(cand_names[taken], score_text[taken])]
        merged_df.loc[targets[not_closest], '一致方法'] = [
            f'一致なし (候補にもっと近い商品が他にある: {c} / 類似度: {s}%)' for c, s in zip(cand_names[not_closest], score_text[not_closest])]

        merged_df.loc[targets[kuji], '一致方法'] = [
            f'一致なし (くじ景品とプライズの違い: {c} / 類似度: {s}%)' for c, s in zip(cand_names[kuji], score_text[kuji])]
        merged_df.loc[targets[conflict], '一致方法'] = [
            f'一致なし (数字が違う候補のみ: {c} / 類似度: {s}%)' for c, s in zip(cand_names[conflict], score_text[conflict])]

        merged_df.loc[targets[ok], MATCHED_COL] = cand_names[ok]
        merged_df.loc[targets[ok], '一致方法'] = [f'厳格あいまい一致 (類似度: {s}%)' for s in score_text[ok]]

        merged_df.loc[targets[auto], MATCHED_COL] = cand_names[auto]
        merged_df.loc[targets[auto], '一致方法'] = [f'自動判定: 相互最良一致 (類似度: {s}%)' for s in score_text[auto]]

        # 要確認は確定させず、候補として別の列に出す
        merged_df.loc[targets[review], CANDIDATE_COL] = cand_names[review]
        merged_df.loc[targets[review], '一致方法'] = [
            f'要確認 (片方にだけ数字あり, 類似度: {s}%)' if d else f'要確認 (類似度: {s}%)'
            for s, d in zip(score_text[review], demoted[review])]

        print(f"  あいまい一致: {ok.sum()}件 / 自動判定: {auto.sum()}件 / 要確認: {review.sum()}件"
              f" / 一致なし: {(~ok & ~auto & ~review).sum()}件（うち数字が違う候補のみ {conflict.sum()}件、くじとプライズの違い {kuji.sum()}件、"
              f"候補が一致済み {taken.sum()}件、候補にもっと近い商品あり {not_closest.sum()}件）")

    return merged_df


def combined_scores(queries, queries_sorted, choices, choices_sorted):
    """詰めた文字列どうしの類似度と、語順をそろえた文字列どうしの類似度の、高いほうを使う"""
    a = process.cdist(queries, choices, scorer=fuzz.WRatio, dtype=np.float32, workers=-1)
    b = process.cdist(queries_sorted, choices_sorted, scorer=fuzz.ratio, dtype=np.float32, workers=-1)
    return np.maximum(a, b)


def overwrite_tab(sh, title, data_to_write):
    """タブを削除せずに中身だけ上書きする（参照・書式・フィルタを保持）。なければ作成する"""
    rows, cols = len(data_to_write), len(data_to_write[0])
    try:
        ws = sh.worksheet(title)
        print(f"既存のタブ「{title}」の内容を上書きしています...")
        ws.clear()
        # 右側に余白の列を残しておく（列を広げたり、メモ用の列を足したりできるように）
        if ws.row_count < rows or ws.col_count < cols + SPARE_COLS:
            ws.resize(rows=max(ws.row_count, rows), cols=max(ws.col_count, cols + SPARE_COLS))
    except gspread.exceptions.WorksheetNotFound:
        print(f"新しいタブ「{title}」を作成して書き込んでいます...")
        ws = sh.add_worksheet(title=title, rows=rows + 10, cols=cols + SPARE_COLS)

    ws.update(range_name='A1', values=data_to_write)
    return ws


def read_tab(sh, title):
    """タブの全セルを文字列で読む（タブがなければ空リスト）"""
    try:
        return sh.worksheet(title).get_all_values()
    except gspread.exceptions.WorksheetNotFound:
        return []


def load_manual(sh, jan_col, master_name_col, manual_sheet=MANUAL_SHEET_NAME, manual_col=MANUAL_COL):
    """手動対応表を読み、前回の結果タブの「手動指定」列での変更を反映した 手動指定の辞書を返す"""
    manual = {}
    for row in read_tab(sh, manual_sheet)[1:]:
        if len(row) >= 3 and row[0].strip() and row[2].strip():
            manual[row[0].strip()] = row[2].strip()

    # 結果タブの「手動指定」列は手動対応表の内容で毎回埋めて出力しているので、
    # 入力されていれば登録・変更、空になっていれば削除として扱う
    values = read_tab(sh, OUTPUT_SHEET_NAME)
    if values and manual_col in values[0]:
        header = values[0]
        mi = header.index(manual_col)
        ji = header.index(jan_col) if jan_col in header else None
        ni = header.index(master_name_col) if master_name_col in header else None
        if ni is not None:
            before = dict(manual)
            for row in values[1:]:
                row = row + [''] * (len(header) - len(row))
                key = make_key(row[ji] if ji is not None else None, row[ni])
                if not key:
                    continue
                v = row[mi].strip()
                if v:
                    manual[key] = v
                else:
                    manual.pop(key, None)
            added = sum(1 for k in manual if before.get(k) != manual[k])
            removed = sum(1 for k in before if k not in manual)
            print(f"結果タブの「{manual_col}」を取り込みました（登録・変更: {added}件 / 削除: {removed}件）")
    return manual


def save_manual(sh, manual, merged_df, jan_col, master_name_col, manual_sheet=MANUAL_SHEET_NAME):
    """手動指定を手動対応表タブに保存する（次回以降の実行でも引き継ぐため）"""
    names = {}
    for j, n in zip(merged_df[jan_col] if jan_col else [None] * len(merged_df), merged_df[master_name_col]):
        names.setdefault(make_key(j, n), str(n))
    data = [['キー(JANまたは商品名)', 'マスター商品名', '価格表側の商品名']]
    data += [[k, names.get(k, ''), v] for k, v in sorted(manual.items())]
    overwrite_tab(sh, manual_sheet, data)


def write_name_list(sh, df_price, price_name_col, list_sheet=LIST_SHEET_NAME):
    """プルダウンと検索サイドバーの選択肢として、価格表の商品名をマスター側のタブにコピーする
    （入力規則では別のスプレッドシートの範囲を参照できないため）"""
    names = dict.fromkeys(n for n in df_price[price_name_col].astype(str).str.strip() if n)
    # 以前の版で書いていた価格列が残らないよう、2列目は空で上書きする
    overwrite_tab(sh, list_sheet, [['価格表の商品名', ''], [NO_MATCH, '']] + [[n, ''] for n in names])


def write_result(sh, merged_df, dropdowns=((MANUAL_COL, LIST_SHEET_NAME),)):
    """結果タブを上書きし、各「手動指定」列に対応する価格表の商品名のプルダウンを付ける
    dropdowns: (手動指定の列名, 選択肢のタブ名) の組"""
    merged_df = merged_df.fillna('').astype(str)
    data_to_write = [merged_df.columns.tolist()] + merged_df.values.tolist()
    ws = overwrite_tab(sh, OUTPUT_SHEET_NAME, data_to_write)
    print(f"::notice::result tab written: rows={len(data_to_write)} cols={len(data_to_write[0])} sheet_cols={ws.col_count}")

    requests = []
    for col, list_sheet in dropdowns:
        if col not in merged_df.columns:
            continue
        ci = merged_df.columns.get_loc(col)
        requests.append({'setDataValidation': {
            'range': {'sheetId': ws.id, 'startRowIndex': 1, 'endRowIndex': len(data_to_write),
                      'startColumnIndex': ci, 'endColumnIndex': ci + 1},
            'rule': {
                'condition': {'type': 'ONE_OF_RANGE', 'values': [{'userEnteredValue': f"='{list_sheet}'!$A$2:$A"}]},
                'showCustomUi': True,
                # 価格表から消えた商品名が残っていても警告表示にとどめる
                'strict': False,
            },
        }})
    if requests:
        sh.batch_update({'requests': requests})


def parse_price(value):
    """「¥1,200」「1200円」などを数値にする。数値にできなければ None"""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, (int, float)):
        return value
    s = re.sub(r'[¥￥,，円\s]', '', str(value))
    try:
        n = float(s)
    except ValueError:
        return None
    return int(n) if n.is_integer() else n


def price_lookup(df_price, name_col, label):
    """価格表の 商品名→買取価格 の辞書。価格の列が見つからなければ空"""
    price_col = next((c for c in df_price.columns if '価格' in c), None)
    if price_col is None:
        print(f"[{label}] 価格の列が見つからないため、価格比較では価格を空欄にします。")
        print(f"::warning::{label}: price column not found")
        return {}
    # 突合と同じく、同じ商品名が複数あれば最後の行を使う
    return {n: parse_price(p) for n, p in zip(df_price[name_col].astype(str).str.strip(), df_price[price_col]) if n}


def write_compare(sh, merged_df, jan_col, master_name_col, stores):
    """価格比較タブを書き出す。1店舗以上で商品名の対応が確定した商品だけを載せる
    stores: (店舗名, 結果の中のその店舗の商品名の列, 商品名→価格 の辞書) の組"""
    header = ([jan_col] if jan_col else []) + [master_name_col]
    for label, _, _ in stores:
        header += [f'{label}_商品名', f'{label}_買取価格']
    header += ['最高値の店舗', '最高値']

    rows = []
    for _, r in merged_df.iterrows():
        names = [str(r[col]).strip() for _, col, _ in stores]
        if not any(names):
            continue
        row = ([str(r[jan_col])] if jan_col else []) + [str(r[master_name_col])]
        prices = []
        for (label, _, lookup), name in zip(stores, names):
            p = lookup.get(name) if name else None
            row += [name, '' if p is None else p]
            prices.append((label, p))
        known = [(label, p) for label, p in prices if p is not None]
        top = max((p for _, p in known), default=None)
        row += ['・'.join(label for label, p in known if p == top) if top is not None else '', '' if top is None else top]
        rows.append(row)

    overwrite_tab(sh, COMPARE_SHEET_NAME, [header] + rows)
    print(f"価格比較: {len(rows)}件")


def sheet_key(value):
    """スプレッドシートのIDを取り出す（URLを丸ごと渡されてもよい）"""
    m = re.search(r'/spreadsheets/d/([A-Za-z0-9_-]+)', value)
    return m.group(1) if m else value.strip()


def records_df(values):
    """シートの値（1行目が見出し）をDataFrameにする。見出しが空の列は捨て、同じ見出しが重複したら後ろに _2, _3 を付ける
    （get_all_records は見出しの空欄や重複があるとエラーになるため）"""
    if not values:
        return pd.DataFrame()
    header = [str(h).strip() for h in values[0]]
    keep, names, seen = [], [], {}
    for i, h in enumerate(header):
        if not h:
            continue
        seen[h] = seen.get(h, 0) + 1
        keep.append(i)
        names.append(h if seen[h] == 1 else f'{h}_{seen[h]}')
    rows = [[(r[i] if i < len(r) else '') for i in keep] for r in values[1:]]
    rows = [r for r in rows if any(str(v).strip() for v in r)]
    df = pd.DataFrame(rows, columns=names)
    # get_all_records と同じく、数字だけのセルは数値にする（JANなどの先頭0が消えないよう、変換は価格の列だけ）
    for c in df.columns:
        if '価格' in c:
            df[c] = df[c].map(lambda v: parse_price(v) if str(v).strip() else '')
    return df


def load_price_df(gc, sheet_id, tab=EXTRA_PRICE_TAB, label=''):
    """価格表を読む。指定のタブがなければ最初のタブ。掲載終了(削除)の行は除く"""
    sh = gc.open_by_key(sheet_key(sheet_id))
    try:
        ws = sh.worksheet(tab)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.get_worksheet(0)
        tabs = [w.title for w in sh.worksheets()]
        print(f"[{label}] 「{tab}」タブがないため、最初のタブ「{ws.title}」を読みます（タブ一覧: {tabs}）")
        print(f"::warning::{label}: tab '{tab}' not found; read '{ws.title}' instead. tabs={tabs}")
    df = records_df(ws.get_all_values())
    print(f"[{label}] タブ「{ws.title}」: {len(df)}行 / 列 {list(df.columns)}")
    if df.empty:
        return df
    df.columns = df.columns.str.strip()
    if EXTRA_STATUS_COL in df.columns:
        df = df[df[EXTRA_STATUS_COL].astype(str).str.strip() != EXTRA_REMOVED]
    # 作品名が別の列に分かれている表（ムーラン）は、作品名を商品名の前につなげて照合する
    name_col = next((c for c in df.columns if '商品名' in c), None)
    if name_col and WORK_COL in df.columns:
        df[name_col] = [n if (not str(w).strip() or str(w).strip() in str(n)) else f'{str(w).strip()} {n}'
                        for w, n in zip(df[WORK_COL], df[name_col])]
    updated_col = next((c for c in df.columns if UPDATED_COL in c), None)  # 「更新日」「更新日時」
    if updated_col:
        # 同じ商品名が何度も載る記録形式の表では、同名なら最後の行を使うので、更新日の古い順に並べておく
        # 日付として読めない行は先頭（＝古い扱い）にする
        dates = pd.to_datetime(df[updated_col].astype(str).str.strip(), errors='coerce', format='mixed')
        df = df.loc[dates.sort_values(kind='stable', na_position='first').index]
    return df.reset_index(drop=True)


def add_extra_matches(merged_df, df_master, df_extra, label, threshold, review_threshold, manual=None):
    """追加店舗の価格表と突合し、「<店舗>_商品名」などの列を結果の右側に追加する
    manual: その店舗用の手動指定（キー→その店舗の価格表の商品名）"""
    jan_col, master_name_col, _ = find_columns(df_master, df_extra)
    keep = [c for c in (jan_col, master_name_col) if c]
    # マスターの他の列と名前がぶつからないよう、JANと商品名だけで突合する
    sub = match_dataframes(df_master[keep].copy(), df_extra, threshold, review_threshold, manual)

    merged_df[f'{label}_商品名'] = sub[MATCHED_COL].values
    merged_df[f'{label}_一致方法'] = sub['一致方法'].values
    merged_df[f'{label}_{CANDIDATE_COL}'] = sub[CANDIDATE_COL].values
    merged_df[extra_names(label)[0]] = sub[MANUAL_COL].values

    hit = (sub[MATCHED_COL].astype(str).str.strip() != '').sum()
    review = (sub[CANDIDATE_COL].astype(str).str.strip() != '').sum()
    print(f"[{label}] 一致: {hit}件 / 要確認: {review}件 / 価格表 {len(df_extra)}件")
    print(f"::notice::extra source {len(df_extra)} rows, matched={hit}, review={review}")
    return merged_df


def execute_sheet_matching(master_sheet_id, price_sheet_id, threshold=MATCH_THRESHOLD, review_threshold=REVIEW_THRESHOLD):
    gc = get_client()
    print("Googleスプレッドシートからデータを取得しています...")

    master_sh = gc.open_by_key(sheet_key(master_sheet_id))
    df_master = pd.DataFrame(master_sh.get_worksheet(0).get_all_records())
    df_master.columns = df_master.columns.str.strip()

    # メインの価格表も追加店舗と同じく「現在一覧」タブ（なければ最初のタブ）から、削除済みを除いて読む
    df_price = load_price_df(gc, price_sheet_id, label=MAIN_LABEL)
    if df_price.empty:
        raise ValueError("メインの価格表が空です。PRICE_ID のスプレッドシートを確認してください。")

    print(f"マスターデータ件数: {len(df_master)}件")
    print(f"買取価格表件数: {len(df_price)}件")

    jan_col, master_name_col, price_name_col = find_columns(df_master, df_price)
    manual = load_manual(master_sh, jan_col, master_name_col)

    merged_df = match_dataframes(df_master, df_price, threshold, review_threshold, manual)

    dropdowns = [(MANUAL_COL, LIST_SHEET_NAME)]
    extra_done = []
    for label, env_name, tab in EXTRA_PRICE_SOURCES:
        sheet_id = os.environ.get(env_name, '').strip()
        if not sheet_id:
            print(f"[{label}] {env_name} が未設定のため飛ばします。")
            print(f"::warning::{env_name} is not set; skipped")
            continue
        print(f"[{label}] 買取価格表を取得しています...")
        df_extra = load_price_df(gc, sheet_id, tab, label)
        if df_extra.empty:
            print(f"[{label}] 価格表が空のため飛ばします。")
            print(f"::warning::{env_name}: price sheet is empty; skipped")
            continue
        manual_col, manual_sheet, list_sheet = extra_names(label)
        # 結果タブを上書きする前に、その店舗の手動指定を読んでおく
        extra_manual = load_manual(master_sh, jan_col, master_name_col, manual_sheet, manual_col)
        merged_df = add_extra_matches(merged_df, df_master, df_extra, label, threshold, review_threshold, extra_manual)
        extra_done.append((label, df_extra, extra_manual))
        dropdowns.append((manual_col, list_sheet))

    # 結果タブを上書きする前に、取り込んだ手動指定を保存しておく
    save_manual(master_sh, manual, merged_df, jan_col, master_name_col)
    write_name_list(master_sh, df_price, price_name_col)
    stores = [(MAIN_LABEL, MATCHED_COL, price_lookup(df_price, price_name_col, MAIN_LABEL))]
    for label, df_extra, extra_manual in extra_done:
        _, manual_sheet, list_sheet = extra_names(label)
        extra_name_col = find_columns(df_master, df_extra)[2]
        save_manual(master_sh, extra_manual, merged_df, jan_col, master_name_col, manual_sheet)
        write_name_list(master_sh, df_extra, extra_name_col, list_sheet)
        stores.append((label, f'{label}_商品名', price_lookup(df_extra, extra_name_col, label)))
    write_result(master_sh, merged_df, dropdowns)
    write_compare(master_sh, merged_df, jan_col, master_name_col, stores)
    print("すべての処理が完了しました！")


if __name__ == "__main__":
    if not MASTER_ID or not PRICE_ID:
        raise ValueError("スプレッドシートのIDが環境変数に設定されていません。")
    execute_sheet_matching(MASTER_ID, PRICE_ID)
