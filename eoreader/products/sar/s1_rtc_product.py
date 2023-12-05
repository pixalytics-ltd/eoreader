# -*- coding: utf-8 -*-
# Copyright 2023, SERTIT-ICube - France, https://sertit.unistra.fr/
# This file is part of eoreader project
#     https://github.com/sertit/eoreader
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
S1 RTC products.
Take a look
`here <https://hyp3-docs.asf.alaska.edu/guides/rtc_product_guide/#readme-file>`_.
"""
import asyncio
import logging
from datetime import datetime
from enum import unique
from typing import Union

import geopandas as gpd
import planetary_computer
from lxml import etree
from pystac import Item
from rasterio import crs
from sertit import AnyPath, path, vectors, xml
from sertit.misc import ListEnum
from sertit.types import AnyPathStrType
from shapely import box

from eoreader import DATETIME_FMT, EOREADER_NAME, cache
from eoreader.bands import SarBandNames as sab
from eoreader.exceptions import InvalidProductError
from eoreader.products import S1SensorMode, SarProduct, StacProduct
from eoreader.products.product import OrbitDirection
from eoreader.utils import simplify

LOGGER = logging.getLogger(EOREADER_NAME)


@unique
class S1RtcProductType(ListEnum):
    """
    S1 RTC products type: RTC
    """

    RTC = "RTC"
    """
    RTC product type.
    https://hyp3-docs.asf.alaska.edu/guides/rtc_product_guide
    """


class S1RtcAsfProduct(SarProduct):
    """
    Class for S1 RTC from Asf
    Take a look
    `here <https://hyp3-docs.asf.alaska.edu/guides/rtc_product_guide/#readme-file>`_.
    """

    def _set_pixel_size(self) -> None:
        """
        Set product default pixel size (in meters)
        """
        try:
            pixel_size = float(self.split_name[4][-2:])
        except ValueError:
            raise InvalidProductError("Incorrect name format!")

        default_res = {
            S1SensorMode.SM: 9.0,
            S1SensorMode.IW: 20.0,
            S1SensorMode.EW: 50.0,
            S1SensorMode.WV: 51.0,
        }

        self.pixel_size = pixel_size
        self.resolution = default_res[self.sensor_mode]

    def _set_instrument(self) -> None:
        """
        Set instrument

        Sentinel-1: https://sentinels.copernicus.eu/web/sentinel/missions/sentinel-1/instrument-payload
        """
        self.instrument = "SAR C-band"

    def _pre_init(self, **kwargs) -> None:
        """
        Function used to pre_init the products
        (setting needs_extraction and so on)
        """
        self._band_folder = self.path

        # Already ORTHO, so OK
        self.needs_extraction = False

        # Its original filename is its name
        self._use_filename = True

        # Pre init done by the super class
        super()._pre_init(**kwargs)

    def _post_init(self, **kwargs) -> None:
        """
        Function used to post_init the products
        (setting product-type, band names and so on)
        """
        # Private attributes
        self._raw_band_regex = "*_{}.tif"

        # Post init done by the super class
        super()._post_init(**kwargs)

    @cache
    def extent(self) -> gpd.GeoDataFrame:
        """
        Get UTM extent of the tile

        .. code-block:: python

            >>> from eoreader.reader import Reader
            >>> path = r"S2A_MSIL1C_20200824T110631_N0209_R137_T30TTK_20200824T150432.SAFE.zip"
            >>> prod = Reader().open(path)
            >>> prod.utm_extent()
                                                        geometry
            0  POLYGON ((309780.000 4390200.000, 309780.000 4...

        Returns:
            gpd.GeoDataFrame: Extent in UTM
        """
        footprint = self.footprint()
        return gpd.GeoDataFrame(
            geometry=[box(*footprint.total_bounds)], crs=footprint.crs
        )

    @cache
    @simplify
    def footprint(self) -> gpd.GeoDataFrame:
        """
        Get UTM footprint of the products (without nodata, *in french == emprise utile*)

        .. code-block:: python

            >>> from eoreader.reader import Reader
            >>> path = r"S2A_MSIL1C_20200824T110631_N0209_R137_T30TTK_20200824T150432.SAFE.zip"
            >>> prod = Reader().open(path)
            >>> prod.footprint()
               index                                           geometry
            0      0  POLYGON ((199980.000 4500000.000, 199980.000 4...

        Returns:
            gpd.GeoDataFrame: Footprint as a GeoDataFrame
        """
        # Footprint is always in UTM
        # https://hyp3-docs.asf.alaska.edu/guides/rtc_product_guide/#image-files
        # Products are distributed as GeoTIFFs (one for each available polarization) projected to the appropriate UTM Zone for the location of the scene.
        if self.is_archived:
            footprint = vectors.read(self.path, archive_regex=r".*\.shp")
        else:
            footprint = vectors.read(next(self.path.glob("*.shp")))

        return footprint

    @cache
    def crs(self) -> crs.CRS:
        """
        Get UTM projection

        .. code-block:: python

            >>> from eoreader.reader import Reader
            >>> path = r"S1A_IW_GRDH_1SDV_20191215T060906_20191215T060931_030355_0378F7_3696.zip"
            >>> prod = Reader().open(path)
            >>> prod.utm_crs()
            CRS.from_epsg(32630)

        Returns:
            crs.CRS: CRS object
        """
        # Products are always in UTM
        # https://hyp3-docs.asf.alaska.edu/guides/rtc_product_guide/#image-files
        # Products are distributed as GeoTIFFs (one for each available polarization) projected to the appropriate UTM Zone for the location of the scene.

        # Estimate UTM from extent
        return self.footprint().crs

    def _set_product_type(self) -> None:
        """Set products type"""
        self.product_type = S1RtcProductType.RTC

    def _set_sensor_mode(self) -> None:
        """Get sensor mode"""
        mode = self.split_name[1]
        # Mono swath SM
        if mode in ["S1", "S2", "S3", "S4", "S5", "S6"]:
            mode = "SM"

        # Get sensor mode
        self.sensor_mode = S1SensorMode.from_value(mode)

        if not self.sensor_mode:
            raise InvalidProductError(
                f"Invalid {self.constellation.value} name: {self.name}"
            )

    def get_datetime(self, as_datetime: bool = False) -> Union[str, datetime]:
        """
        Get the product's acquisition datetime, with format :code:`YYYYMMDDTHHMMSS` <-> :code:`%Y%m%dT%H%M%S`

        .. code-block:: python

            >>> from eoreader.reader import Reader
            >>> path = r"1011117-766193"
            >>> prod = Reader().open(path)
            >>> prod.get_datetime(as_datetime=True)
            datetime.datetime(2020, 10, 28, 22, 46, 25)
            >>> prod.get_datetime(as_datetime=False)
            '20201028T224625'

        Args:
            as_datetime (bool): Return the date as a datetime.datetime. If false, returns a string.

        Returns:
             Union[str, datetime.datetime]: Its acquisition datetime
        """
        if self.datetime is None:
            # Sentinel-2 datetime (in the filename) is the datatake sensing time, not the granule sensing time !
            sensing_time = self.split_name[2]

            # Convert to datetime
            date = datetime.strptime(sensing_time, "%Y%m%dT%H%M%S")
        else:
            date = self.datetime

        if not as_datetime:
            date = date.strftime(DATETIME_FMT)

        return date

    def _get_name_constellation_specific(self) -> str:
        """
        Set product real name from metadata

        Returns:
            str: True name of the product (from metadata)
        """
        name = path.get_filename(self.get_quicklook_path())
        if "rgb" in name:
            name = name.replace("_rgb", "")

        return name

    @cache
    def _read_mtd(self) -> (etree._Element, dict):
        """
        Read metadata and outputs the metadata XML root and its namespaces as a dict

        .. code-block:: python

            >>> from eoreader.reader import Reader
            >>> path = r"1001513-735093"
            >>> prod = Reader().open(path)
            >>> prod.read_mtd()
            (<Element DeliveryNote at 0x2454ad4ee88>, {})

        Returns:
            (etree._Element, dict): Metadata XML root and its namespaces
        """
        # No MTD!
        return None, {}

    def get_quicklook_path(self) -> str:
        """
        Get quicklook path if existing.

        Returns:
            str: Quicklook path
        """
        quicklook_path = None
        try:
            if self.is_archived:
                quicklook_path = self.path / path.get_archived_path(
                    self.path, file_regex=r".*_rgb\.png"
                )
            else:
                quicklook_path = next(self.path.glob("*_rgb.png"))
        except (StopIteration, FileNotFoundError):
            try:
                if self.is_archived:
                    quicklook_path = self.path / path.get_archived_path(
                        self.path, file_regex=r".*\.png"
                    )
                else:
                    quicklook_path = next(self.path.glob("*.png"))
            except (StopIteration, FileNotFoundError):
                pass

        return str(quicklook_path)

    @cache
    def get_orbit_direction(self) -> OrbitDirection:
        """
        Get cloud cover as given in the metadata

        .. code-block:: python

            >>> from eoreader.reader import Reader
            >>> path = r"S2A_MSIL1C_20200824T110631_N0209_R137_T30TTK_20200824T150432.SAFE.zip"
            >>> prod = Reader().open(path)
            >>> prod.get_orbit_direction().value
            "DESCENDING"

        Returns:
            OrbitDirection: Orbit direction (ASCENDING/DESCENDING)
        """
        return OrbitDirection.UNKNOWN


class S1RtcMpcStacProduct(StacProduct, SarProduct):
    """
    Class for S1 RTC from MPC (via stac)
    Take a look
    `here <https://planetarycomputer.microsoft.com/dataset/sentinel-1-rtc>`_.
    """

    def __init__(
        self,
        product_path: AnyPathStrType = None,
        archive_path: AnyPathStrType = None,
        output_path: AnyPathStrType = None,
        remove_tmp: bool = False,
        **kwargs,
    ) -> None:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        self.kwargs = kwargs
        """Custom kwargs"""

        # Copy the kwargs
        super_kwargs = kwargs.copy()

        # Get STAC Item
        self.item = None
        """ STAC Item of the product """
        self.item = super_kwargs.pop("item", None)
        if self.item is None:
            try:
                self.item = Item.from_file(product_path)
            except TypeError:
                raise InvalidProductError(
                    "You should either fill 'product_path' or 'item'."
                )

        self.default_clients = [planetary_computer]
        self.clients = super_kwargs.pop("client", self.default_clients)

        if product_path is None:
            # Canonical link is always the second one
            # TODO: check if ok
            product_path = AnyPath(self.item.links[0].target).parent

        # Initialization from the super class
        super().__init__(product_path, archive_path, output_path, remove_tmp, **kwargs)

    def _set_pixel_size(self) -> None:
        """
        Set product default pixel size (in meters)
        """
        # Use range, maybe azimuth one day
        self.pixel_size = self.item.properties["sar:pixel_spacing_range"]
        self.resolution = self.item.properties["sar:resolution_range"]

    def _set_instrument(self) -> None:
        """
        Set instrument

        Sentinel-1: https://sentinels.copernicus.eu/web/sentinel/missions/sentinel-1/instrument-payload
        """
        self.instrument = "SAR C-band"

    def _pre_init(self, **kwargs) -> None:
        """
        Function used to pre_init the products
        (setting needs_extraction and so on)
        """
        self.stac_mtd = self.item.to_dict()

        self._band_folder = self.path

        # Already ORTHO, so OK
        self.needs_extraction = False

        # Its original filename is its name
        self._use_filename = True

        # Pre init done by the super class
        super()._pre_init(**kwargs)

    def _post_init(self, **kwargs) -> None:
        """
        Function used to post_init the products
        (setting product-type, band names and so on)
        """
        # Private attributes
        self._raw_band_regex = "*_{!l}.rtc.tiff"

        # Post init done by the super class
        super()._post_init(**kwargs)

    def get_raw_band_paths(self, **kwargs) -> dict:
        """
        Return the existing band paths (as they come with the archived products).

        Args:
            **kwargs: Additional arguments

        Returns:
            dict: Dictionary containing the path of every band existing in the raw products
        """
        band_paths = {}
        for band in sab.speckle_list():
            try:
                band_paths[band] = self.sign_url(
                    self.item.assets[band.name.lower()].href
                )
            except KeyError:
                continue

        return band_paths

    def _set_product_type(self) -> None:
        """Set products type"""
        self.product_type = S1RtcProductType.RTC

    def _set_sensor_mode(self) -> None:
        """Get sensor mode"""
        mode = self.split_name[1]
        # Mono swath SM
        if mode in ["S1", "S2", "S3", "S4", "S5", "S6"]:
            mode = "SM"

        # Get sensor mode
        self.sensor_mode = S1SensorMode.from_value(mode)

        if not self.sensor_mode:
            raise InvalidProductError(
                f"Invalid {self.constellation.value} name: {self.name}"
            )

    def get_datetime(self, as_datetime: bool = False) -> Union[str, datetime]:
        """
        Get the product's acquisition datetime, with format :code:`YYYYMMDDTHHMMSS` <-> :code:`%Y%m%dT%H%M%S`

        .. code-block:: python

            >>> from eoreader.reader import Reader
            >>> path = r"1011117-766193"
            >>> prod = Reader().open(path)
            >>> prod.get_datetime(as_datetime=True)
            datetime.datetime(2020, 10, 28, 22, 46, 25)
            >>> prod.get_datetime(as_datetime=False)
            '20201028T224625'

        Args:
            as_datetime (bool): Return the date as a datetime.datetime. If false, returns a string.

        Returns:
             Union[str, datetime.datetime]: Its acquisition datetime
        """
        if self.datetime is None:
            # Sentinel-2 datetime (in the filename) is the datatake sensing time, not the granule sensing time !
            sensing_time = self.split_name[4]

            # Convert to datetime
            date = datetime.strptime(sensing_time, "%Y%m%dT%H%M%S")
        else:
            date = self.datetime

        if not as_datetime:
            date = date.strftime(DATETIME_FMT)

        return date

    def _get_name(self) -> str:
        """
        Set product real name.

        Returns:
            str: True name of the product (from metadata)
        """
        return self.stac_mtd["id"]

    def _get_name_constellation_specific(self) -> str:
        """
        Set product real name from metadata

        Returns:
            str: True name of the product (from metadata)
        """
        # No need here (_get_name reimplemented)
        pass

    @cache
    def _read_mtd(self) -> (etree._Element, dict):
        """
        Read metadata and outputs the metadata XML root and its namespaces as a dict

        .. code-block:: python

            >>> from eoreader.reader import Reader
            >>> path = r"1001513-735093"
            >>> prod = Reader().open(path)
            >>> prod.read_mtd()
            (<Element DeliveryNote at 0x2454ad4ee88>, {})

        Returns:
            (etree._Element, dict): Metadata XML root and its namespaces
        """
        return xml.dict_to_xml(self.stac_mtd), {}

    def get_quicklook_path(self) -> str:
        """
        Get quicklook path if existing.

        Returns:
            str: Quicklook path
        """
        quicklook_path = None
        try:
            if self.is_archived:
                quicklook_path = self.path / path.get_archived_path(
                    self.path, file_regex=r".*preview\.png"
                )
            else:
                quicklook_path = next(self.path.glob("*preview.png"))
        except (StopIteration, FileNotFoundError):
            pass

        return str(quicklook_path)

    @cache
    def get_orbit_direction(self) -> OrbitDirection:
        """
        Get cloud cover as given in the metadata

        .. code-block:: python

            >>> from eoreader.reader import Reader
            >>> path = r"S2A_MSIL1C_20200824T110631_N0209_R137_T30TTK_20200824T150432.SAFE.zip"
            >>> prod = Reader().open(path)
            >>> prod.get_orbit_direction().value
            "DESCENDING"

        Returns:
            OrbitDirection: Orbit direction (ASCENDING/DESCENDING)
        """
        return OrbitDirection.from_value(
            self.item.properties["sat:orbit_state"].upper()
        )
