# -*- coding: utf-8 -*-
# 引入依赖包
# pip install alibabacloud_imageseg20191230

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


    download_path=download_image(url)
    time.sleep(1)
    #场景一：文件在本地
    stream = open(download_path, 'rb')
    segment_common_image_request.image_urlobject = stream

    #场景二：使用任意可访问的url
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
      res=str(response.body)
      res2=res.replace("'","\"")

      ImageURL=json.loads(res2)["Data"]["ImageURL"]
      print("生成白底图链接:",ImageURL)
      return ImageURL

    except Exception as error:
      # 获取整体报错信息
      print(error)
      # 获取单个字段
      print(error.code)
      # tips: 可通过error.__dict__查看属性名称

    #关闭流
    stream.close()
    os.remove(download_path)

if __name__ == '__main__':
    get_whiteBk("https://cbu01.alicdn.com/img/ibank/O1CN01oagu0Z20X1DkQ2TVU_!!2218151486858-0-cib.jpg")