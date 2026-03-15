import requests
import os


def download_image(url, save_path='downloaded_image.jpg'):
    """
    从指定URL下载图片并保存到本地

    参数:
        url (str): 图片的URL地址
        save_path (str): 保存图片的路径和文件名，默认为'downloaded_image.jpg'

    返回:
        bool: 下载成功返回True，失败返回False
    """
    try:

        list = url.split("/")

        # 示例图片URL
        path = "E:\\pictures\\" + list[-1]  # 保存路径
        save_path=path.split("?")[0]
        # 发送HTTP请求获取图片
        response = requests.get(url, stream=True)

        # 检查请求是否成功
        response.raise_for_status()

        # 确保保存目录存在
        directory = os.path.dirname(save_path)
        if directory and not os.path.exists(directory):
            os.makedirs(directory)

        # 写入图片文件
        with open(save_path, 'wb') as file:
            for chunk in response.iter_content(chunk_size=8192):
                file.write(chunk)

        print(f"图片下载成功，已保存至: {save_path}")
        return save_path

    except requests.exceptions.HTTPError as e:
        print(f"HTTP错误: {e}")
    except requests.exceptions.ConnectionError:
        print("连接错误，请检查网络连接或URL是否正确")
    except requests.exceptions.Timeout:
        print("请求超时")
    except requests.exceptions.RequestException as e:
        print(f"下载失败: {e}")
    except IOError as e:
        print(f"文件写入错误: {e}")
    except Exception as e:
        print(e)

    return save_path


if __name__ == "__main__":
    # 示例用法
    image_url = "https://cbu01.alicdn.com/img/ibank/O1CN01x7cV2c1Wh9at0A9dw_!!2201234142819-0-cib.jpg_sum.jpg"

    # 执行下载
    download_image(image_url)