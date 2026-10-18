// マスター側のスプレッドシートに貼り付けて使う、手動指定用の検索サイドバー
// タブ名・列名は jan_price_matcher.py の定数と合わせること
const RESULT_SHEET = '突合結果_自動出力';
const LIST_SHEET = '価格表_商品名リスト';
const MANUAL_COL = '手動指定';
const NO_MATCH = '(一致させない)';

// 指定後に「次の行へ」移動するとき、フィルタで隠れた行を何行まで飛ばすか
const MAX_SKIP_ROWS = 2000;

function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('突合')
    .addItem('商品を検索して指定', 'showSearchSidebar')
    .addToUi();
}

function showSearchSidebar() {
  const html = HtmlService.createHtmlOutputFromFile('Sidebar').setTitle('商品を検索');
  SpreadsheetApp.getUi().showSidebar(html);
}

// 価格表の商品名と価格の一覧（サイドバーの検索対象）
function getPriceList() {
  const sheet = SpreadsheetApp.getActive().getSheetByName(LIST_SHEET);
  if (!sheet) throw new Error(`「${LIST_SHEET}」タブがありません。先に GitHub Actions で突合を実行してください。`);
  const last = sheet.getLastRow();
  if (last < 2) return [];
  return sheet.getRange(2, 1, last - 1, 2).getDisplayValues()
    .filter(r => r[0] && r[0] !== NO_MATCH)
    .map(r => ({ name: r[0], price: r[1] }));
}

function resultColumns_(sheet) {
  const header = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getDisplayValues()[0];
  return {
    width: header.length,
    // マスターの列が先に並ぶので、最初に「商品名」を含む列がマスター側の商品名
    name: header.findIndex(c => c.includes('商品名')),
    method: header.indexOf('一致方法'),
    candidate: header.indexOf('価格表側の商品名'),
    manual: header.indexOf(MANUAL_COL),
  };
}

// 選択中の行の情報（サイドバーが定期的に呼ぶ）
function getContext() {
  const sheet = SpreadsheetApp.getActiveSheet();
  if (sheet.getName() !== RESULT_SHEET) {
    return { ok: false, message: `「${RESULT_SHEET}」タブの行を選択してください。` };
  }
  const row = sheet.getActiveRange().getRow();
  if (row < 2) return { ok: false, message: 'データの行を選択してください。' };

  const cols = resultColumns_(sheet);
  if (cols.manual < 0) {
    return { ok: false, message: `「${MANUAL_COL}」列がありません。先に GitHub Actions で突合を実行してください。` };
  }
  const v = sheet.getRange(row, 1, 1, cols.width).getDisplayValues()[0];
  return {
    ok: true,
    row: row,
    name: cols.name >= 0 ? v[cols.name] : '',
    method: cols.method >= 0 ? v[cols.method] : '',
    candidate: cols.candidate >= 0 ? v[cols.candidate] : '',
    manual: v[cols.manual],
  };
}

// 指定した行の「手動指定」に書き込む。moveNext なら次の表示行を選択する
function setManual(row, value, moveNext) {
  const sheet = SpreadsheetApp.getActive().getSheetByName(RESULT_SHEET);
  const cols = sheet ? resultColumns_(sheet) : null;
  if (!cols || cols.manual < 0 || row < 2) throw new Error('書き込み先の行が見つかりません。');

  const cell = sheet.getRange(row, cols.manual + 1);
  // 「=」などで始まる商品名が数式として解釈されないようにする
  cell.setValue(/^[=+\-@]/.test(value) ? "'" + value : value);

  if (moveNext) {
    const last = sheet.getLastRow();
    let r = row + 1;
    while (r <= last && r - row <= MAX_SKIP_ROWS && (sheet.isRowHiddenByFilter(r) || sheet.isRowHiddenByUser(r))) r++;
    if (r <= last && r - row <= MAX_SKIP_ROWS) sheet.setActiveRange(sheet.getRange(r, cols.manual + 1));
  }
}
