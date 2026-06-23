# -*- coding: utf-8 -*-
# 引入依赖包
# pip install alibabacloud_imageseg20191230
from publish import *
import os
import io
import time
from urllib.request import urlopen
from alibabacloud_imageseg20191230.client import Client
from alibabacloud_imageseg20191230.models import SegmentCommonImageAdvanceRequest
from alibabacloud_tea_openapi.models import Config
from alibabacloud_tea_util.models import RuntimeOptions
import json
from Utils import download_image


def get_whiteBk(url):
    config = Config(
        # 创建AccessKey ID和AccessKey Secret，请参考https://help.aliyun.com/document_detail/175144.html。
        # 如果您用的是RAM用户的AccessKey，还需要为RAM用户授予权限AliyunVIAPIFullAccess，请参考https://help.aliyun.com/document_detail/145025.html。
        # 从环境变量读取配置的AccessKey ID和AccessKey Secret。运行代码示例前必须先配置环境变量。
        access_key_id=os.environ.get('ALIBABA_CLOUD_ACCESS_KEY_ID'),
        access_key_secret=os.environ.get('ALIBABA_CLOUD_ACCESS_KEY_SECRET'),
        # 访问的域名。
        endpoint='imageseg.cn-shanghai.aliyuncs.com',
        # 访问的域名对应的region
        region_id='cn-shanghai'
    )
    segment_common_image_request = SegmentCommonImageAdvanceRequest()

    download_path = download_image(url)
    time.sleep(1)
    # 场景一：文件在本地
    stream = open(download_path, 'rb')
    segment_common_image_request.image_urlobject = stream

    # 场景二：使用任意可访问的url
    # url=url
    # img = urlopen(url).read()
    # segment_common_image_request.image_urlobject = io.BytesIO(img)
    segment_common_image_request.return_form = 'whiteBK'

    runtime = RuntimeOptions()
    try:
        # 初始化Client
        client = Client(config)
        response = client.segment_common_image_advance(segment_common_image_request, runtime)
        # 获取整体结果
        res = str(response.body)
        res2 = res.replace("'", "\"")

        ImageURL = json.loads(res2)["Data"]["ImageURL"]
        print("生成白底图链接:", ImageURL)
        return ImageURL

    except Exception as error:
        # 获取整体报错信息
        print(error)
        # 获取单个字段
        print(error.code)
        # tips: 可通过error.__dict__查看属性名称

    # 关闭流
    stream.close()
    os.remove(download_path)


def post_picture(url, ACCESS_TOKEN):
    # 下载图片到本地
    print("下载的图片url为", url)
    path = download_image(url)
    # 读取图片
    image = Image.open(path)

    # 设置图片大小
    new_image = image.resize((800, 800))
    print(path)
    new_path = path.split(".")[-1] + "001.jpg"
    new_image.save(new_path)

    failpath = path.split(".")[-1] + ""

    headers = {
        'Authorization': 'Bearer ' + ACCESS_TOKEN,
        # requests won't add a boundary if this header is set when you pass files=
        # 'content-type': 'multipart/form-data',
    }
    try:
        with open(new_path, 'rb') as f:
            files = {'file': f}
            response = requests.post(
                'https://api.mercadolibre.com/pictures/items/upload',
                headers=headers,
                files=files
            )
        print("上传图片到美客多:", response.json())
        id = response.json()['id']
        url = response.json()["variations"][0]['url']
        return id, url
    except Exception as e:
        print("图片上传美客多失败:" + url)
        new_image.save(new_path)
    finally:
        if os.path.exists(new_path):
            os.remove(new_path)
            print(f"已删除本地临时文件: {new_path}")


# def download_image(url, save_path='downloaded_image.jpg'):
#     """
#     从指定URL下载图片并保存到本地
#
#     参数:
#         url (str): 图片的URL地址
#         save_path (str): 保存图片的路径和文件名，默认为'downloaded_image.jpg'
#
#     返回:
#         bool: 下载成功返回True，失败返回False
#     """
#     try:
#
#         list = url.split("/")
#
#         # 示例图片URL
#         path = "E:\\pictures\\" + list[-1]  # 保存路径
#         save_path = path.split("?")[0]
#         # 发送HTTP请求获取图片
#         response = requests.get(url, stream=True)
#
#         # 检查请求是否成功
#         response.raise_for_status()
#
#         # 确保保存目录存在
#         directory = os.path.dirname(save_path)
#         if directory and not os.path.exists(directory):
#             os.makedirs(directory)
#
#         # 写入图片文件
#         with open(save_path, 'wb') as file:
#             for chunk in response.iter_content(chunk_size=8192):
#                 file.write(chunk)
#
#         print(f"图片下载成功，已保存至: {save_path}")
#         return save_path
#
#     except requests.exceptions.HTTPError as e:
#         print(f"HTTP错误: {e}")
#     except requests.exceptions.ConnectionError:
#         print("连接错误，请检查网络连接或URL是否正确")
#     except requests.exceptions.Timeout:
#         print("请求超时")
#     except requests.exceptions.RequestException as e:
#         print(f"下载失败: {e}")
#     except IOError as e:
#         print(f"文件写入错误: {e}")
#     except Exception as e:
#         print(e)
#
#     return save_path



def download_image(url, save_path=None):
    """
    改进版下载函数
    """
    try:
        # 1. 智能处理保存路径
        if save_path is None:
            # 从URL提取文件名，并去除URL参数（如 ?token=...）
            filename = url.split("/")[-1].split("?")[0]
            # 建议不要写死 E:\，换成相对路径或配置路径
            save_path = os.path.join("downloads", filename)

        # 2. 确保目录存在
        directory = os.path.dirname(save_path)
        if directory and not os.path.exists(directory):
            os.makedirs(directory)

        # 3. 发送请求 (加入超时控制)
        # 很多服务器拒绝没有 User-Agent 的请求，所以最好加上 Headers
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        response = requests.get(url, stream=True, timeout=15, headers=headers)

        # 检查状态码
        response.raise_for_status()

        # 4. 写入文件
        with open(save_path, 'wb') as file:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:  # 过滤掉保持连接的 chunk
                    file.write(chunk)

        print(f"✅ 下载成功: {save_path}")
        return save_path

    except Exception as e:
        # 强制打印具体的异常类型和内容，防止“静默失败”
        print(f"❌ 下载失败！URL: {url}")
        print(f"   错误类型: {type(e).__name__}")
        print(f"   详细错误: {e}")
        return None


# 测试调用
# download_image("https://example.com/image.jpg")


def upload_pictures_first(url, token):
    bk_url = get_whiteBk(url)
    id, mercado_url = post_picture(bk_url, token)
    print(id)
    print(mercado_url)
    return id, mercado_url


def upload_pictures(url, token):
    id, mercado_url = post_picture(url, token)
    print(id)
    print(mercado_url)
    return id, mercado_url


if __name__ == '__main__':
    # bk_url = get_whiteBk("https://http2.mlstatic.com/D_803764-CBT104004493105_012026-F.jpg")
    # token = getToken()
    # id, mercado_url = post_picture(bk_url, token)
    # print(id)
    # print(mercado_url)
    path=download_image("https://cbu01.alicdn.com/img/ibank/O1CN01DA0O0z1Js3s7qPjXb_!!992791083-0-cib.jpg")
    image = Image.open(path)

    # 设置图片大小
    new_image = image.resize((800, 800))
    print(path)