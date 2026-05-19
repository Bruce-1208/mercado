import logging
from typing import Optional
from decimal import Decimal
from datetime import datetime
from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, Field
from db_pool import get_db_connection  # 假设你把连接池封装在 db_pool.py 中
import uvicorn

# 初始化 FastAPI 应用
app = FastAPI(
    title="电商数据采集与大模型识别数据接收接口",
    description="用于接收并存储 1688 找货、克重修改以及大模型置信度等映射记录",
    version="1.0.0"
)

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


# --- 1. 定义请求体数据模型 (Pydantic) ---
# 完美对应数据库字段，自带类型校验
class RecordInsertModel(BaseModel):
    zhiying_category: Optional[str] = Field(None, description="智赢分类", max_length=100)
    original_img_url: Optional[str] = Field(None, description="原图链接", max_length=1024)
    is_same_style: int = Field(0, description="是否同款：0-未确认，1-是，2-否", ge=0, le=2)
    product_id: str = Field(..., description="产品编号", max_length=50)
    title: Optional[str] = Field(None, description="标题", max_length=500)
    identified_weight: int = Field(0, description="识别克重(g)")
    pre_modified_weight: int = Field(0, description="修改前克重(g)")
    post_modified_weight: int = Field(0, description="修改后克重(g)")
    pre_modified_cost_usd: Decimal = Field(Decimal('0.0000'), description="修改前成本价(USD)")
    post_modified_cost_usd: Decimal = Field(Decimal('0.0000'), description="修改后成本价(USD)")
    max_sku_price_cny: Decimal = Field(Decimal('0.00'), description="最高SKU价(CNY)")
    max_sku_spec: Optional[str] = Field(None, description="最高SKU规格", max_length=255)
    max_sku_id: Optional[str] = Field(None, description="最高SKU ID", max_length=100)
    model_confidence: Decimal = Field(Decimal('0.00'), description="大模型置信度")
    weight_issue: Optional[str] = Field(None, description="克重问题描述", max_length=255)
    matched_1688_url: Optional[str] = Field(None, description="匹配1688链接", max_length=1024)
    reason: Optional[str] = Field(None, description="原因/备注说明")

    class Config:
        # 允许使用 Decimal 等类型
        json_encoders = {
            Decimal: lambda v: float(v)
        }


# --- 2. 编写核心插入接口 ---
@app.post("/api/v1/records", status_code=status.HTTP_201_CREATED, summary="单条数据插入")
async def insert_record(data: RecordInsertModel):
    sql = """
        INSERT INTO product_mapping_records (
            crawl_time, zhiying_category, original_img_url, is_same_style, 
            product_id, title, identified_weight, pre_modified_weight, 
            post_modified_weight, pre_modified_cost_usd, post_modified_cost_usd, 
            max_sku_price_cny, max_sku_spec, max_sku_id, model_confidence, 
            weight_issue, matched_1688_url, reason
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
        )
    """

    current_time = datetime.now()
    params = (
        current_time, data.zhiying_category, data.original_img_url, data.is_same_style,
        data.product_id, data.title, data.identified_weight, data.pre_modified_weight,
        data.post_modified_weight, data.pre_modified_cost_usd, data.post_modified_cost_usd,
        data.max_sku_price_cny, data.max_sku_spec, data.max_sku_id, data.model_confidence,
        data.weight_issue, data.matched_1688_url, data.reason
    )

    conn = None
    cursor = None
    try:
        # 从连接池获取连接
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(sql, params)
        conn.commit()

        logging.info(f"成功录入产品数据，Product ID: {data.product_id}")
        return {"status": "success", "message": "Record inserted successfully", "id": cursor.lastrowid}

    except Exception as e:
        if conn:
            conn.rollback()
        logging.error(f"数据库写入失败: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database error: {str(e)}"
        )
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()  # 这里的 close 是把连接放回连接池，而不是真正关闭


if __name__ == "__main__":


    # 启动服务，运行在本地 8000 端口
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)