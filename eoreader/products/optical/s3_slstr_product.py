# -*- coding: utf-8 -*-
# Copyright 2021, SERTIT-ICube - France, https://sertit.unistra.fr/
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
Sentinel-3 SLSTR products

.. WARNING:
    Not georeferenced NetCDF files are badly opened by GDAL and therefore by rasterio !
    -> use xr.open_dataset that manages that correctly
"""
import logging
from collections import defaultdict, namedtuple
from functools import reduce
from pathlib import Path
from typing import Union

import numpy as np
import xarray as xr
from cloudpathlib import CloudPath
from rasterio import features
from rasterio.enums import Resampling
from sertit import rasters, rasters_rio
from sertit.misc import ListEnum
from sertit.rasters import MAX_CORES, XDS_TYPE
from sertit.vectors import WGS84

from eoreader import utils
from eoreader.bands.alias import ALL_CLOUDS, CIRRUS, CLOUDS, RAW_CLOUDS
from eoreader.bands.bands import BandNames
from eoreader.bands.bands import OpticalBandNames as obn
from eoreader.exceptions import InvalidProductError, InvalidTypeError
from eoreader.keywords import SLSTR_RAD_ADJUST
from eoreader.products.optical.s3_product import (
    S3DataType,
    S3Instrument,
    S3Product,
    S3ProductType,
)
from eoreader.reader import Platform
from eoreader.utils import EOREADER_NAME

LOGGER = logging.getLogger(EOREADER_NAME)

# FROM SNAP (only for radiance bands, not for brilliance temperatures)
# https://github.com/senbox-org/s3tbx/blob/197c9a471002eb2ec1fbd54e9a31bfc963446645/s3tbx-rad2refl/src/main/java/org/esa/s3tbx/processor/rad2refl/Rad2ReflConstants.java#L141
# Not used for now
SLSTR_SOLAR_FLUXES_DEFAULT = {
    obn.GREEN: 1837.39,
    obn.RED: 1525.94,
    obn.NIR: 956.17,
    obn.NARROW_NIR: 956.17,
    obn.SWIR_CIRRUS: 365.90,
    obn.SWIR_1: 248.33,
    obn.SWIR_2: 78.33,
}
SUFFIX_500m = ["an", "ao", "bn", "bo"]
"""
- "an" and "ao" refer to the 500 m grid, stripe A, respectively for nadir view (n) and oblique view (o)
- "bn" and "bo" refer to the 500 m grid, stripe B
"""

SUFFIX_1km = ["in", "io", "fn", "fo"]
"""
- "in" and "io" refer to the 1 km grid
- "fn" and "fo" refer to the F1 channel 1 km grid
"""
SLSTR_SUFFIX = SUFFIX_500m + SUFFIX_1km

# Create
SLSTR_RAD_BANDS = ["S1", "S2", "S3", "S4", "S5", "S6"]
SLSTR_BT_BANDS = ["S7", "S8", "S9", "F1", "F2"]

# Nadir and Oblique
FIELDS = [f"{rad}_n" for rad in SLSTR_RAD_BANDS] + [
    f"{rad}_o" for rad in SLSTR_RAD_BANDS
]
SlstrRadAdjustTuple = namedtuple(
    "SlstrRadAdjustTuple", FIELDS, defaults=(1.0,) * len(FIELDS)
)


class SlstrRadAdjust(ListEnum):
    """
    SLSTR Radiance Adjustment dictionaries.

    Sentinel-3 SLSTR radiometry is not nominal, therefore a first-order radiometric correction is provided.
    """

    SNAP = SlstrRadAdjustTuple(
        # Nadir
        S5_n=1.12,
        S6_n=1.13,
        # Oblique
        S5_o=1.15,
        S6_o=1.14,
    )
    """
    SNAP Radiometric adjustment used in S3MPC Adjustment (optional in SNAP). Coefficients can be seen
    [here](https://github.com/senbox-org/s3tbx/blob/b10514e399f7a8a436002d2bacdb0c62be72f8f8/s3tbx-sentinel3-reader/src/main/java/org/esa/s3tbx/dataio/s3/slstr/SlstrLevel1ProductFactory.java#L72-L75)
    """

    S3_PN_SLSTR_L1_06 = SlstrRadAdjustTuple(
        # Nadir
        S5_n=1.12,
        S6_n=1.15,
        # Oblique
        S5_o=1.20,
        S6_o=1.26,
    )
    """
    Coefficients given in the
    [Sentinel-3 Product Notice 06](https://www-cdn.eumetsat.int/files/2020-04/pdf_s3a_pn_slstr_l1_06.pdf),
    edited the 07/11/2018 and reviewed the 19/11/2018
    """

    S3_PN_SLSTR_L1_07 = S3_PN_SLSTR_L1_06
    """
    Coefficients given in the
    [Sentinel-3 Product Notice 07](https://www-cdn.eumetsat.int/files/2020-06/pdf_s3a_pn_slstr_l1_07_1.1.pdf),
    edited the 15/01/2020 and reviewed the 09/06/2020, same as the Product Notice 06.
    """

    S3_PN_SLSTR_L1_08 = SlstrRadAdjustTuple(
        # Nadir
        S1_n=0.97,
        S2_n=0.98,
        S3_n=0.98,
        S5_n=1.11,
        S6_n=1.13,
        # Oblique
        S1_o=0.94,
        S2_o=0.95,
        S3_o=0.95,
        S5_o=1.04,
        S6_o=1.07,
    )
    """
    Coefficients given in the
    [Sentinel-3 Product Notice 08](https://www-cdn.eumetsat.int/files/2021-05/S3.PN-SLSTR-L1.08%20-%20i1r0%20-%20SLSTR%20L1%20PB%202.75-A%20and%201.53-B.pdf),
    edited the 18/05/2021.

    The default one.
    """

    NONE = SlstrRadAdjustTuple()
    """
    Coefficients set to one.
    """


class S3SlstrProduct(S3Product):
    """
    Class of Sentinel-3 SLSTR Products
    """

    def __init__(
        self,
        product_path: Union[str, CloudPath, Path],
        archive_path: Union[str, CloudPath, Path] = None,
        output_path: Union[str, CloudPath, Path] = None,
        remove_tmp: bool = False,
    ) -> None:

        """
        https://sentinels.copernicus.eu/web/sentinel/technical-guides/sentinel-3-slstr/level-1/observation-mode-desc
        Note that the name of each netCDF file provides information about it's content.
        The suffix of each filename is associated with the selected grid:
            "an" and "ao" refer to the 500 m grid, stripe A, respectively for nadir view (n) and oblique view (o)
            "bn" and "bo" refer to the 500 m grid, stripe B
            "in" and "io" refer to the 1 km grid
            "fn" and "fo" refer to the F1 channel 1 km grid
            "tx/n/o" refer to the tie-point grid for agnostic/nadir and oblique view
        """
        self._flags_file = None
        self._cloud_name = None
        self._exception_name = None
        self._suffix = "an"

        super().__init__(
            product_path, archive_path, output_path, remove_tmp
        )  # Order is important here

        self._gcps = defaultdict(list)

    def _pre_init(self) -> None:
        """
        Function used to pre_init the products
        (setting needs_extraction and so on)
        """
        self.needs_extraction = False

        # Post init done by the super class
        super()._pre_init()

    def _get_platform(self) -> Platform:
        """ Getter of the platform """
        # look in the MTD to be sure
        root, _ = self.read_mtd()
        name = root.findtext(".//product_name")

        if "SL" in name:
            # Instrument
            self._instrument = S3Instrument.SLSTR
            sat_id = self._instrument.value
        else:
            raise InvalidProductError(
                f"Only OLCI and SLSTR are valid Sentinel-3 instruments : {self.name}"
            )

        return getattr(Platform, sat_id)

    def change_suffix(self, new_suffix: str) -> None:
        """
        Changing the file [suffix]
        (https://sentinels.copernicus.eu/web/sentinel/technical-guides/sentinel-3-slstr/level-1/observation-mode-desc)

        Note that the name of each netCDF file provides information about it's content.
        The suffix of each filename is associated with the selected grid:
        - "an" and "ao" refer to the 500 m grid, stripe A, respectively for nadir view (n) and oblique view (o)
        - "bn" and "bo" refer to the 500 m grid, stripe B
        - "in" and "io" refer to the 1 km grid
        - "fn" and "fo" refer to the F1 channel 1 km grid
        - "tx/n/o" refer to the tie-point grid for agnostic/nadir and oblique view

        Args:
            new_suffix (str): New suffix (accepted ones: `an`, `ao`, `bn`, `bo`, `in`, `io`, `fn`, `fo`)
        """
        assert new_suffix in SLSTR_SUFFIX
        self._suffix = new_suffix
        self._set_preprocess_members(new_suffix)

    def _set_preprocess_members(self, suffix: str = "an"):
        """
        Set pre-process members.

        Initialize suffix to an

        Args:
            suffix (str): Suffix

        Returns:

        """
        assert suffix in SLSTR_SUFFIX
        # Radiance bands
        self._radiance_file = f"{{}}_radiance_{suffix}.nc"
        self._radiance_subds = f"{{}}_radiance_{suffix}"

        # Geocoding
        self._geo_file = f"geodetic_{suffix}.nc"
        self._lat_nc_name = f"latitude_{suffix}"
        self._lon_nc_name = f"longitude_{suffix}"
        self._alt_nc_name = f"elevation_{suffix}"

        # Tie geocoding
        self._tie_geo_file = "geodetic_tx.nc"
        self._tie_lat_nc_name = "latitude_tx"
        self._tie_lon_nc_name = "longitude_tx"

        # Mean Sun angles
        self._geom_file = f"geometry_t{suffix[-1]}.nc"
        self._saa_name = f"solar_azimuth_t{suffix[-1]}"
        self._sza_name = f"solar_zenith_t{suffix[-1]}"

        # Rad 2 Refl
        self._misc_file = f"{{}}_quality_{suffix}.nc"
        self._solar_flux_name = f"{{}}_solar_irradiance_{suffix}"

        # Clouds
        self._flags_file = f"flags_{suffix}.nc"
        self._cloud_name = f"cloud_{suffix}"

        # Other
        self._exception_name = f"{{}}_exception_{suffix}"

    def _set_resolution(self) -> float:
        """
        Set product default resolution (in meters)
        """
        return 500.0

    def _set_product_type(self) -> None:
        """Set products type"""
        # Product type
        if self.name[7] != "1":
            raise InvalidTypeError("Only L1 products are used for Sentinel-3 data.")

        self.product_type = S3ProductType.SLSTR_RBT
        self._data_type = S3DataType.RBT

        # Bands
        self.band_names.map_bands(
            {
                obn.GREEN: SLSTR_RAD_BANDS[0],  # S1, radiance, 500m
                obn.RED: SLSTR_RAD_BANDS[1],  # S2, radiance, 500m
                obn.NIR: SLSTR_RAD_BANDS[2],  # S3, radiance, 500m
                obn.NARROW_NIR: SLSTR_RAD_BANDS[3],  # S3, radiance, 500m
                obn.SWIR_CIRRUS: SLSTR_RAD_BANDS[3],  # S4, radiance, 500m
                obn.SWIR_1: SLSTR_RAD_BANDS[4],  # S5, radiance, 500m
                obn.SWIR_2: SLSTR_RAD_BANDS[5],  # S6, radiance, 500m
                # TODO: convert BT to radiance to use it
                # SLSTR_BT_BANDS[0]: SLSTR_BT_BANDS[0],  # S7, brilliance temperature, 1km
                # obn.TIR_1: SLSTR_BT_BANDS[1],  # S8, brilliance temperature, 1km
                # obn.TIR_2: SLSTR_BT_BANDS[2],  # S9, brilliance temperature, 1km
                # SLSTR_BT_BANDS[3]: SLSTR_BT_BANDS[3],  # F1, brilliance temperature, 1km
                # SLSTR_BT_BANDS[4]: "SLSTR_BT_BANDS[4],  # F2, brilliance temperature, 1km
            }
        )

    def _preprocess(
        self,
        band: Union[obn, str],
        resolution: float = None,
        to_reflectance: bool = True,
        subdataset: str = None,
        **kwargs,
    ) -> Union[CloudPath, Path]:
        """
        Pre-process S3 SLSTR bands:
        - Geocode
        - Adjust radiance
        - Convert radiance to reflectance

        Args:
            band (Union[obn, str]): Band to preprocess (quality flags or others are accepted)
            resolution (float): Resolution
            to_reflectance (bool): Convert band to reflectance
            subdataset (str): Subdataset
            kwargs: Other arguments used to load bands

        Returns:
            dict: Dictionary containing {band: path}
        """
        band_str = band if isinstance(band, str) else band.value

        path = self._get_preprocessed_band_path(band, resolution=resolution)

        if not path.is_file():
            path = self._get_preprocessed_band_path(
                band, resolution=resolution, writable=True
            )

            # Get raw band
            band_arr = self._read_nc(band, subdataset)

            # Adjust radiance if needed
            # Get the user's radiance adjustment if existing
            rad_adjust = kwargs.get(SLSTR_RAD_ADJUST, SlstrRadAdjust.S3_PN_SLSTR_L1_08)
            assert isinstance(rad_adjust, SlstrRadAdjust)
            band_arr = self._radiance_adjustment(band_arr, band, rad_adjust=rad_adjust)

            # Convert radiance to reflectances if needed
            # Convert first pixel by pixel before reprojection !
            if to_reflectance:
                LOGGER.debug(f"Converting {band_str} to reflectance")
                band_arr = self._rad_2_refl(band_arr, band)

                # Debug
                utils.write(
                    band_arr,
                    self._get_band_folder(writable=True).joinpath(
                        f"{self.condensed_name}_{band.name}_rad2refl.tif"
                    ),
                )

            # Geocode
            if isinstance(band, str):
                suffix = band.split(".")[0][-2:]
            elif subdataset is not None:
                suffix = subdataset.split(".")[0][-2:]
            else:
                suffix = self._suffix
            LOGGER.debug(f"Geocoding {band_str}")
            pp_arr = self._geocode(band_arr, resolution=resolution, suffix=suffix)

            # Write on disk
            utils.write(pp_arr, path)

        return path

    def _create_gcps(self, suffix: str) -> None:
        """
        Create the GCPs sequence (WGS84)
        """
        if suffix not in self._gcps and not self._gcps[suffix]:
            geo_file = f"geodetic_{suffix}.nc"
            lon_nc_name = f"longitude_{suffix}"
            lat_nc_name = f"latitude_{suffix}"
            alt_nc_name = f"elevation_{suffix}"

            # Open cartesian files to populate the GCPs
            lat = self._read_nc(geo_file, lat_nc_name)
            lon = self._read_nc(geo_file, lon_nc_name)
            alt = self._read_nc(geo_file, alt_nc_name)

            # Create GCPs
            self._gcps[suffix] = utils.create_gcps(lon, lat, alt)

    def _geocode(
        self, band_arr: xr.DataArray, resolution: float = None, suffix: str = None
    ) -> xr.DataArray:
        """
        Geocode Sentinel-3 SLSTR bands (using cartesian coordinates)

        Args:
            band_arr (xr.DataArray): Band array
            resolution (float): Resolution
            suffix (str): Suffix (for the grid)

        Returns:
            xr.DataArray: Geocoded DataArray
        """
        if not suffix:
            suffix = self._suffix

        # Create GCPs if not existing
        self._create_gcps(suffix)

        # Assign a projection
        band_arr.rio.write_crs(WGS84, inplace=True)

        return band_arr.rio.reproject(
            dst_crs=self.crs,
            resolution=resolution,
            gcps=self._gcps[suffix],
            nodata=self.nodata,
            num_threads=MAX_CORES,
            **{"SRC_METHOD": "GCP_TPS"},
        )

    def _tie_to_img(self, tie_arr: np.ndarray, suffix: str) -> np.ndarray:
        """
        Convert an image sampled on the tie point grid (tx) to the wanted gris, given by the suffix

        Args:
            tie_arr (xr.Dataset): Image sampled on the tie point grid (tx)
            suffix: Suffix of the new grid

        Returns:
            np.ndarray: Array resampled to the wanted grid as a numpy array
        """
        # Load tie point grid
        tie_cart_file = "cartesian_tx.nc"
        tx_nc_name = "x_tx"
        ty_nc_name = "y_tx"

        # WARNING: RectBivariateSpline must have increasing values
        tx = np.squeeze(self._read_nc(tie_cart_file, tx_nc_name).data)[0, ::-1]
        ty = np.squeeze(self._read_nc(tie_cart_file, ty_nc_name).data)[:, 0]

        # Load fill image grid (cartesian)
        geo_file = f"cartesian_{suffix}.nc"
        fx_nc_name = f"x_{suffix}"
        fy_nc_name = f"y_{suffix}"

        fx = np.squeeze(self._read_nc(geo_file, fx_nc_name))
        fy = np.squeeze(self._read_nc(geo_file, fy_nc_name))

        # Interpolate via Spline (as extrapolation is possible and the grid is very sparse along the rows)
        # Import scipy here (long import)
        from scipy.interpolate import RectBivariateSpline

        # WARNING 2: Rasterio reads like [count, y, x] !
        no_nan_arr = np.nan_to_num(np.squeeze(tie_arr).data[:, ::-1])
        spline_interp = RectBivariateSpline(ty, tx, no_nan_arr)

        # Interpolate and set nodata back
        img_arr = spline_interp.ev(fy, fx)
        img_arr[img_arr == 0] = np.nan

        return img_arr

    def _bt_2_rad(self, band_arr: xr.DataArray, band: obn = None) -> xr.DataArray:
        """
        Convert brightness temperature to radiance

        The Level-1 brightness temperature measurements provided for the thermal channels (S7-S9, F1 and F2)
        can be converted to radiance by integrating the Planck function at the BT of interest multiplied over the
        spectral response of each band. The spectral response functions for SLSTR-A and SLSTR-B are available on
        the ESA Sentinel Online website (see Section 8.2.10)
        https://sentinel.esa.int/web/sentinel/technical-guides/sentinel-3-slstr/instrument/measured-spectral-response-function-data

        In https://sentinel.esa.int/documents/247904/4598085/Sentinel-3-SLSTR-Land-Handbook.pdf/bee342eb-40d4-9b31-babb-8bea2748264a
        Args:
            band_arr (xr.DataArray): Band array
            band (obn): Optical Band

        Returns:
            dict: Dictionary containing {band: path}
        """

        return band_arr

    def _rad_2_refl(self, band_arr: xr.DataArray, band: obn = None) -> xr.DataArray:
        """
        Convert radiance to reflectance

        The visible and SWIR channels (S1-S6) provide measurements of top of atmosphere (ToA) radiances
        (mW/m2/sr/nm). These values can be converted to normalised reflectance for better comparison or
        merging of data with different sun angles as follows:
        reflectance = π* (ToA radiance / solar irradiance / COS(solar zenith angle))
        where the solar irradiance at ToA is given in the ‘quality’ dataset for the channel,
        and the solar zenith angle is given in the ‘geometry’ dataset.

        The solar irradiance contained in the quality dataset is derived from the solar spectrum
        of Thuillier et al. (2003) integrated over the measured SLSTR spectral responses
        and corrected for the earth-to-sun distance at the time of the measurement.

        In https://sentinel.esa.int/documents/247904/4598085/Sentinel-3-SLSTR-Land-Handbook.pdf/bee342eb-40d4-9b31-babb-8bea2748264a

        Args:
            band_arr (xr.DataArray): Band array
            band (obn): Optical Band

        Returns:
            dict: Dictionary containing {band: path}
        """
        rad_2_refl_path = self._get_band_folder() / f"rad_2_refl_{band.name}.npy"

        if not rad_2_refl_path.is_file():
            rad_2_refl_path = (
                self._get_band_folder(writable=True) / f"rad_2_refl_{band.name}.npy"
            )

            # Open SZA array (resampled to band_arr size)
            sza = self._compute_sza_img_grid()

            # Open solar flux (resampled to band_arr size)
            e0 = self._compute_e0(band)

            # Compute rad_2_refl coeff
            rad_2_refl_coeff = (np.pi / e0 / np.cos(sza)).astype(np.float32)

            # Write on disk
            np.save(rad_2_refl_path, rad_2_refl_coeff)

        else:
            # Open rad_2_refl_coeff (resampled to band_arr size)
            rad_2_refl_coeff = np.load(rad_2_refl_path)

        return band_arr * rad_2_refl_coeff

    def _radiance_adjustment(
        self,
        band_arr: xr.DataArray,
        band: obn = None,
        rad_adjust: SlstrRadAdjust = SlstrRadAdjust.S3_PN_SLSTR_L1_08,
    ) -> xr.DataArray:
        """
        Applying the radiance adjustment as recommended in the product notice:
        S3.PN-SLSTR-L1.08 (https://www-cdn.eumetsat.int/files/2021-05/S3.PN-SLSTR-L1.08%20-%20i1r0%20-%20SLSTR%20L1%20PB%202.75-A%20and%201.53-B.pdf):

        SLSTR-A/B: All solar channels (S1-S6) have been undergoing a vicarious calibration assessment to
        quantify their radiometric calibration adjustment. Recent analysis of vicarious calibration results
        over desert sites performed by RAL, CNES, Rayference and University of Arizona have determined
        new and consistent radiometric deviations wrt. common reference sensors (MERIS, MODIS)
        [S3MPC.RAL.TN.010]. Consequently, these have been used to provide a first-order radiometric
        corrections which are provided in the below tables with more detail at the following link
        [S3MPC.RAL.TN.020]. Current radiances in the L1B product remain uncorrected of these
        radiometric calibration adjustments. Hence, these multiplicative coefficients are strongly
        recommended to be used by all users.

        Nadir view
                      S1   S2   S3   S5   S6
        Correction  0.97 0.98 0.98 1.11 1.13
        Uncertainty 0.03 0.02 0.02 0.02 0.02

        Oblique view
                      S1   S2   S3   S5   S6
        Correction  0.94 0.95 0.95 1.04 1.07
        Uncertainty 0.05 0.03 0.03 0.03 0.05

        Args:
            band_arr (xr.DataArray): Band array
            band (obn): Optical Band
            rad_adjust (SlstrRadAdjust): Radiance Adjustment

        Returns:
            xr.DataArray: Adjusted band array
        """
        try:
            band_name = self.band_names[band]
            if band_name in SLSTR_RAD_BANDS:
                rad_coeff = getattr(rad_adjust.value, f"{band_name}_{self._suffix[-1]}")
            else:
                # Brilliance temperature
                rad_coeff = 1.0
            return band_arr * rad_coeff
        except KeyError:
            # Not a band (ie Quality Flags)
            return band_arr

    def _compute_sza_img_grid(self) -> np.ndarray:
        """
        Compute Sun Zenith Angle (in radian) resampled to the image grid (from the tie point grid)

        Returns:
            np.ndarray: Resampled Sun Zenith Angle as a numpy array
        """
        sza_img_path = self._get_band_folder() / f"sza_{self._suffix}.npy"
        if not sza_img_path.exists():
            sza_img_path = (
                self._get_band_folder(writable=True) / f"sza_{self._suffix}.npy"
            )
            sza = self._read_nc(self._geom_file, self._sza_name)
            sza_rad = sza * np.pi / 180.0

            # From tie grid to image grid
            sza_img = self._tie_to_img(sza_rad, self._suffix)

            # Write on disk
            np.save(sza_img_path, sza_img)

        else:
            # Open rad_2_refl_coeff (resampled to band_arr size)
            sza_img = np.load(sza_img_path)

        return sza_img

    def _compute_e0(self, band: obn = None) -> np.ndarray:
        """
        Compute the solar spectral flux in mW / (m^2 * sr * nm)

        Args:
            band (obn): Optical Band

        Returns:
            np.ndarray: Solar Flux

        """
        misc = self._misc_file.replace("{}", self.band_names[band])
        solar_flux_name = self._solar_flux_name.replace("{}", self.band_names[band])

        e0 = self._read_nc(misc, solar_flux_name).data
        e0 = np.nanmean(e0)
        if np.isnan(e0):
            e0 = SLSTR_SOLAR_FLUXES_DEFAULT[band]

        return e0

    # pylint: disable=R0913
    # R0913: Too many arguments (6/5) (too-many-arguments)
    def _manage_invalid_pixels(self, band_arr: XDS_TYPE, band: obn) -> XDS_TYPE:
        """
        Manage invalid pixels (Nodata, saturated, defective...)

        ISP_absent pixel_absent not_decompressed no_signal saturation invalid_radiance no_parameters unfilled_pixel"

        Args:
            band_arr (XDS_TYPE): Band array
            band (obn): Band name as an OpticalBandNames

        Returns:
            XDS_TYPE: Cleaned band array
        """
        # Open quality flags
        # NOT OPTIMIZED, MAYBE CHECK INVALID PIXELS ON NOT GEOCODED DATA
        qual_flags_path = self._preprocess(
            band,
            subdataset=self._exception_name.replace("{}", self.band_names[band]),
            resolution=band_arr.rio.resolution(),
            to_reflectance=False,
        )

        # Open flag file
        qual_arr, _ = rasters_rio.read(
            qual_flags_path,
            size=(band_arr.rio.width, band_arr.rio.height),
            resampling=Resampling.nearest,  # Nearest to keep the flags
            masked=False,
        )

        # Set no data for everything (except ISP) that caused an exception
        exception = np.where(qual_arr > 2, self._mask_true, self._mask_false)

        # Get nodata mask
        no_data = np.where(np.isnan(band_arr.data), self._mask_true, self._mask_false)

        # Combine masks
        mask = no_data | exception

        # DO not set 0 to epsilons as they are a part of the
        return self._set_nodata_mask(band_arr, mask)

    def _has_cloud_band(self, band: BandNames) -> bool:
        """
        Does this products has the specified cloud band ?
        -> SLSTR does
        """
        if band in [
            RAW_CLOUDS,
            ALL_CLOUDS,
            CLOUDS,
            CIRRUS,
        ]:
            has_band = True
        else:
            has_band = False

        return has_band

    def _load_clouds(
        self, bands: list, resolution: float = None, size: Union[list, tuple] = None
    ) -> dict:
        """
        Load cloud files as xarrays.

        Read S3 SLSTR clouds from the flags file:cloud netcdf file.
        https://sentinels.copernicus.eu/web/sentinel/technical-guides/sentinel-3-slstr/level-1/cloud-identification

        bit_id  flag_masks (ushort)     flag_meanings
        ===     ===                     ===
        0       1US                     visible
        1       2US                     1.37_threshold
        2       4US                     1.6_small_histogram
        3       8US                     1.6_large_histogram
        4       16US                    2.25_small_histogram
        5       32US                    2.25_large_histogram
        6       64US                    11_spatial_coherence
        7       128US                   gross_cloud
        8       256US                   thin_cirrus
        9       512US                   medium_high
        10      1024US                  fog_low_stratus
        11      2048US                  11_12_view_difference
        12      4096US                  3.7_11_view_difference
        13      8192US                  thermal_histogram
        14      16384US                 spare
        15      32768US                 spare

        Args:
            bands (list): List of the wanted bands
            resolution (int): Band resolution in meters
            size (Union[tuple, list]): Size of the array (width, height). Not used if resolution is provided.
        Returns:
            dict: Dictionary {band_name, band_xarray}
        """
        band_dict = {}

        if bands:
            all_ids = list(np.arange(0, 14))
            cir_id = 8
            cloud_ids = [id for id in all_ids if id != cir_id]

            # Open path
            # TODO
            cloud_path = self._preprocess(
                self._flags_file,
                subdataset=self._cloud_name,
                resolution=resolution,
                to_reflectance=False,
            )

            # Open cloud file
            clouds_array = utils.read(
                cloud_path,
                resolution=resolution,
                size=size,
                resampling=Resampling.nearest,
                masked=False,
            ).astype(np.uint16)

            # Get nodata mask
            nodata = np.where(np.isnan(clouds_array), 1, 0)

            for band in bands:
                if band == ALL_CLOUDS:
                    band_dict[band] = self._create_mask(clouds_array, all_ids, nodata)
                elif band == CLOUDS:
                    band_dict[band] = self._create_mask(clouds_array, cloud_ids, nodata)
                elif band == CIRRUS:
                    band_dict[band] = self._create_mask(clouds_array, cir_id, nodata)
                elif band == RAW_CLOUDS:
                    band_dict[band] = clouds_array
                else:
                    raise InvalidTypeError(
                        f"Non existing cloud band for Sentinel-3 SLSTR: {band}"
                    )

        return band_dict

    def _create_mask(
        self,
        bit_array: xr.DataArray,
        bit_ids: Union[int, list],
        nodata: np.ndarray,
    ) -> xr.DataArray:
        """
        Create a mask masked array (uint8) from a bit array, bit IDs and a nodata mask.

        Args:
            bit_array (xr.DataArray): Conditional array
            bit_ids (Union[int, list]): Bit IDs
            nodata (np.ndarray): Nodata mask

        Returns:
            xr.DataArray: Mask masked array

        """
        if not isinstance(bit_ids, list):
            bit_ids = [bit_ids]
        conds = rasters.read_bit_array(bit_array, bit_ids)
        cond = reduce(lambda x, y: x | y, conds)  # Use every conditions (bitwise or)

        cond_arr = np.where(cond, self._mask_true, self._mask_false).astype(np.uint8)
        cond_arr = np.squeeze(cond_arr)
        try:
            cond_arr = features.sieve(cond_arr, size=10, connectivity=4)
        except TypeError:
            # Manage dask arrays that fails with rasterio sieve
            cond_arr = features.sieve(cond_arr.compute(), size=10, connectivity=4)
        cond_arr = np.expand_dims(cond_arr, axis=0)

        return super()._create_mask(bit_array, cond_arr, nodata)
