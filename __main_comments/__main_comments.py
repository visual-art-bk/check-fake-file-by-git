import os, requests, asyncio, urllib.parse, json, re
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
import google.generativeai as genai
from datetime import datetime
import pandas as pd
import openpyxl
from openpyxl.utils.dataframe import dataframe_to_rows
from openpyxl.styles import Alignment, Font
from PIL import Image
from io import BytesIO
from tqdm.asyncio import tqdm
import nodriver as uc
import absl.logging
from openpyxl.drawing.image import Image as OpenpyxlImage

# nodriver의 에러 메시지를 최소화하기 위해 로그 레벨을 에러로 설정
absl.logging.set_verbosity("error")

# 작업 시간을 유니크한 타임스탬프로 정의해 파일 이름 등에 활용
timestamp = datetime.now().strftime("%Y%m%d%H%M%S")

# URL을 저장한 텍스트 파일을 읽어옴
with open("url.txt", "r") as file:
    urls = file.read().splitlines()


def is_valid_image(img_content):
    """
    이미지가 유효한지 확인하는 함수.
    PIL(Pillow)을 사용하여 이미지 파일을 열고 크기를 확인함.
    유효하지 않거나 높이가 200 미만인 이미지는 False를 반환.
    """
    try:
        img = Image.open(BytesIO(img_content))
        img.verify()  # 이미지 데이터 유효성 검증
        if img.height < 200:  # 이미지 높이 제한 조건
            return False
        return True
    except (IOError, SyntaxError):  # 이미지가 아닌 경우 처리
        return False


results = {}  # URL별 작업 결과를 저장
lock = asyncio.Lock()  # 비동기 작업 간 데이터 충돌 방지를 위한 Lock


async def fetch_page_source(url, folder_path):
    """
    nodriver(비동기 브라우저)를 사용해 지정된 URL의 페이지 소스를 가져오는 함수.
    10초의 타임아웃을 설정하며, 실패 시 '실패'로 결과를 기록.
    """
    browser = await uc.start()  # 비동기 브라우저 세션 시작

    try:
        # 비동기로 페이지를 로드하며 10초의 타임아웃 설정
        page = await asyncio.wait_for(browser.get(url), timeout=10)

        count = 0  # 반복 카운터 초기화
        while True:
            if count > 5:  # 최대 5회 시도 후 실패 처리
                async with lock:
                    results[url]["결과"] = "실패"
                return ""

            await asyncio.sleep(3)  # 페이지가 로드되었는지 기다림
            count += 1

            try:
                # 페이지 소스를 가져오며 5초 타임아웃 설정
                source = await asyncio.wait_for(page.get_content(), timeout=5)
            except asyncio.TimeoutError:
                async with lock:
                    results[url]["결과"] = "실패"
                return ""

            # HTML에 img 태그가 있으면 성공으로 간주하고 반환
            if "<img" in source:
                return source
    except asyncio.TimeoutError:
        async with lock:
            results[url]["결과"] = "실패"
        return ""
    except Exception:
        async with lock:
            results[url]["결과"] = "실패"
        return ""
    finally:
        # 브라우저 세션 종료
        browser.stop()


def parse_images(html_data, url):
    """
    HTML 소스에서 불필요한 태그를 제거한 뒤 img 태그를 찾아내고
    유효한 이미지 URL 리스트를 반환.
    """
    soup = BeautifulSoup(html_data, "html.parser")

    # 헤더, 푸터, 로고, 배너, 카테고리 등 불필요한 태그 제거
    for tag in soup.find_all(["header", "head", "footer"]):
        tag.decompose()
    for tag in soup.find_all(
        class_=lambda class_name: class_name and "recommend" in class_name
    ):
        tag.decompose()
    for tag in soup.find_all(
        class_=lambda class_name: class_name and "relate" in class_name
    ):
        tag.decompose()
    for tag in soup.find_all(
        class_=lambda class_name: class_name and "logo" in class_name
    ):
        tag.decompose()
    for tag in soup.find_all(
        class_=lambda class_name: class_name and "together" in class_name
    ):
        tag.decompose()
    for tag in soup.find_all(
        class_=lambda class_name: class_name and "list" in class_name
    ):
        tag.decompose()
    for tag in soup.find_all(
        class_=lambda class_name: class_name and "review" in class_name
    ):
        tag.decompose()
    for tag in soup.find_all(
        class_=lambda class_name: class_name and "banner" in class_name
    ):
        tag.decompose()
    for tag in soup.find_all(
        class_=lambda class_name: class_name and "category" in class_name
    ):
        tag.decompose()
    for tag in soup.find_all(
        class_=lambda class_name: class_name and "option" in class_name
    ):
        tag.decompose()
    for tag in soup.find_all(
        class_=lambda class_name: class_name and "guide" in class_name
    ):
        tag.decompose()

    # img 태그를 찾고 유효한 src 속성만 추출
    img_tags = soup.find_all("img")
    img_urls = [
        (
            urljoin(url, img["src"])  # 상대 경로는 절대 경로로 변환
            if ";base64," not in img["src"]
            else (
                urljoin(url, img["ec-data-src"]) if "ec-data-src" in img.attrs else ""
            )
        )
        for img in img_tags
        if "src" in img.attrs
        and not img["src"].lower().endswith(".svg")  # SVG 파일 제외
        and not "//img.echosting.cafe24.com/" in img["src"]  # 카페24 기본 이미지 제외
        and "/theme/" not in img["src"]  # 테마 관련 리소스 제외
        and "facebook" not in img["src"]  # SNS 로고 제외
        and "icon" not in img["src"]  # 아이콘 제외
        and "logo" not in img["src"]  # 로고 제외
        and "common" not in img["src"]  # 공통 리소스 제외
        and "banner" not in img["src"]  # 배너 이미지 제외
        and "brand" not in img["src"]  # 브랜드 로고 제외
    ]

    return img_urls  # 유효 이미지 URL 리스트 반환
