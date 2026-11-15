// WebGUI で使う、必要最小限のプログラム(4.2)。
// 機器登録・編集の画面で、チェックを外したコマンドのインターフェース番号の入力欄を、
// 入力できない状態(グレー表示)にする(7.4.4.2、UI-10)。
(function () {
  "use strict";

  function apply(checkbox) {
    var target = document.getElementById(checkbox.getAttribute("data-controls"));
    if (target) {
      target.disabled = !checkbox.checked;
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    var boxes = document.querySelectorAll("input[type=checkbox][data-controls]");
    Array.prototype.forEach.call(boxes, function (box) {
      apply(box);
      box.addEventListener("change", function () { apply(box); });
    });
  });
})();
