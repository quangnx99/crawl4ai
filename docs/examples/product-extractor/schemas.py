from typing import Optional

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class ProductVariant(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    name: str = Field(description="Variant label, e.g. 'Red / XL'")
    price: Optional[str] = Field(
        default=None, description="Price if different from main product"
    )
    sku: Optional[str] = Field(default=None)
    available: Optional[bool] = Field(default=None)


class Product(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: Optional[str] = Field(
        default=None, description="Product ID as shown in the page URL or page content, e.g. 'm81825383935'"
    )
    name: Optional[str] = Field(default=None, description="Product name / title")
    price: Optional[str] = Field(
        default=None,
        description="Numeric price only, no currency symbol or code, e.g. '29.99' or '262500'",
    )
    original_price: Optional[str] = Field(
        default=None,
        description="Numeric original price before discount, no currency symbol or code",
    )
    currency: Optional[str] = Field(
        default=None, description="ISO currency code, e.g. 'USD', 'VND'"
    )
    sku: Optional[str] = Field(default=None, description="SKU or product code")
    brand: Optional[str] = Field(default=None)
    description: Optional[str] = Field(
        default=None, description="Short product description (plain text summary)"
    )
    description_html: Optional[str] = Field(
        default=None,
        description="Full product description as raw HTML from the page",
    )
    images: list[str] = Field(
        default_factory=list, description="List of product image URLs"
    )
    variants: Optional[list[ProductVariant]] = Field(
        default=None, description="Size, color, or other variants"
    )
    is_sold_out: Optional[bool] = Field(
        default=None, description="True if the product is sold out, False if available"
    )
    category: Optional[str] = Field(
        default=None,
        description="Product category path, e.g. 'Electronics > Headphones'",
    )
    seller: Optional[str] = Field(default=None, description="Seller or shop name")
    condition: Optional[str] = Field(
        default=None, description="e.g. 'New', 'Used', 'Refurbished'"
    )
    rating: Optional[str] = Field(default=None, description="e.g. '4.5/5'")
    review_count: Optional[int] = Field(default=None)
    origin_code: Optional[str] = Field(
        default=None,
        description="DEPRECATED alias for ship_from_country. Kept for backward "
        "compatibility with existing consumers. New code should use ship_from_country.",
    )
    ship_from_country: Optional[str] = Field(
        default=None,
        description="ISO 3166-1 alpha-2 code of the country the parcel SHIPS FROM "
        "(seller's warehouse). Used to compute international shipping + proxy fees. "
        "NOT the manufacturing country, NOT the brand HQ. "
        "Example: product made in VN sold on uniqlo.com/jp → ship_from_country = JP.",
    )
    ship_from_evidence: Optional[str] = Field(
        default=None,
        description="Exact quoted snippet from the page supporting ship_from_country "
        "(<= 120 chars). Null when ship_from_country is null or inferred from site context.",
    )
    ship_from_confidence: Optional[str] = Field(
        default=None,
        description="Confidence: 'high' (explicit Ships-from text / seller location), "
        "'medium' (inferred from site locale for single-brand sites), or null.",
    )
    brand_country: Optional[str] = Field(
        default=None,
        description="ISO alpha-2 of brand headquarters country. Informational only — "
        "NOT used for shipping fee calculation.",
    )


class ExtractionRequest(BaseModel):
    url: str = Field(description="Product page URL to extract from")


class ExtractionResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    success: bool
    url: str
    data: Optional[Product] = None
    error: Optional[str] = None
