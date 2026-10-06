import hydromt
import numpy as np




dc = hydromt.DataCatalog(
    data_libs=[
        r"D:\wflow\jawa_merit\HydroMT\data_catalog_jawa.yml"
    ]
)

lulc = dc.get_rasterdataset(
    "esa_worldcover_jawa"
)

print(lulc)

print(
    "Min:",
    lulc.min().values
)

print(
    "Max:",
    lulc.max().values
)
values = np.unique(
    lulc.values
)
print(values)