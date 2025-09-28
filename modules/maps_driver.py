import os
import time
import random
from selenium import webdriver
from selenium.webdriver.chrome.service import Service as ChromeService
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

class MapsDriver:
    """
    Seleniumラッパー（MVP版）
    - Chrome + webdriver-manager
    - 詳細ボタンのクリックと、テキスト抽出のフォールバック
    """
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.driver = None
        self._init_driver()

    def _init_driver(self):
        options = webdriver.ChromeOptions()
        if self.cfg["app"].get("headless", False):
            options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
        options.add_argument("--lang=ja-JP")
        options.add_argument("--window-size=1280,1200")
        # Botっぽさ低減（最低限）
        options.add_argument("--disable-blink-features=AutomationControlled")

        self.driver = webdriver.Chrome(
            service=ChromeService(ChromeDriverManager().install()),
            options=options
        )
        self.wait = WebDriverWait(self.driver, self.cfg["app"]["timeout_sec"])

    # ユーティリティ
    def _rsleep(self, ms_from=200, ms_to=600):
        ms = random.randint(ms_from, ms_to)
        time.sleep(ms / 1000)

    def open(self, url: str):
        self.driver.get(url)
        self._rsleep(*self.cfg["app"]["random_sleep_ms"])
        # 結果の何かが出るまで待機（MVPゆるめ）
        try:
            self.wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        except Exception:
            pass

    def open_details_panel(self):
        """
        「詳細」ボタンを探して押す。UI差分に備えて複数の探し方を試す。
        """
        xpaths = [
            # ボタン要素で「詳細」を含む
            "//button[.//text()[contains(., '詳細')]]",
            "//button[contains(., '詳細')]",
            # 'aria-label' に '詳細'
            "//*[@aria-label][contains(@aria-label, '詳細')]",
            # 英語UIフォールバック
            "//button[contains(., 'Details')]",
        ]
        for xp in xpaths:
            try:
                el = self.wait.until(EC.element_to_be_clickable((By.XPATH, xp)))
                el.click()
                self._rsleep(*self.cfg["app"]["random_sleep_ms"])
                return
            except Exception:
                continue
        # 押せなかった場合は何もしない（後でbodyテキストをフォールバック抽出）

    def get_details_text_fallback(self) -> str:
        """
        右パネルが狙えない場合のフォールバックとして、ページ全体のテキストから
        交通情報っぽい行を中心に拾う。
        """
        body = self.driver.find_element(By.TAG_NAME, "body")
        all_text = body.text or ""
        # ノイズ削減：関連しやすいキーワードを含む行だけ残す
        keep_keys = ["出発", "到着", "所要", "分", "円", "徒歩", "乗換", "方面", "行", "線", "駅", "料金", "運賃"]
        lines = []
        for raw in all_text.splitlines():
            s = raw.strip()
            if not s:
                continue
            if any(k in s for k in keep_keys):
                lines.append(s)
        return "\n".join(lines)

    def close(self):
        try:
            self.driver.quit()
        except Exception:
            pass
