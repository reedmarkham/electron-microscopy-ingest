import os
import sys
import json
import argparse
import logging
from ftplib import FTP
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
import hashlib
from typing import Any, Dict, List

import requests
import numpy as np
from tqdm import tqdm
from ncempy.io import ser
from dm3_lib import _dm3_lib as dm3

# Add lib directory to path for config_manager import  
sys.path.append('/app/lib')
from config_manager import get_config_manager
from metadata_manager import MetadataManager

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


def fetch_metadata(entry_id: str, api_base_url: str) -> Dict[str, Any]:
    api_url = f'{api_base_url}/{entry_id}/'
    response = requests.get(api_url)
    response.raise_for_status()
    return response.json()


def is_file(ftp: FTP, filename: str) -> bool:
    try:
        ftp.size(filename)
        return True
    except:
        return False


def download_files(entry_id: str, download_dir: str, ftp_server: str) -> List[str]:
    ftp = FTP(ftp_server)
    ftp.login()
    ftp.cwd(f'/empiar/world_availability/{entry_id}/data/')
    filenames = ftp.nlst()

    downloaded_files = []
    os.makedirs(download_dir, exist_ok=True)

    # Use tqdm with proper positioning for consistent output
    with tqdm(total=len(filenames), desc="Downloading files",
             bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]",
             position=0, leave=True) as pbar:
        for filename in filenames:
            if not is_file(ftp, filename):
                logger.info("Skipping directory or invalid file: %s", filename)
                pbar.update(1)
                continue
            local_path = os.path.join(download_dir, filename)
            with open(local_path, 'wb') as f:
                ftp.retrbinary(f'RETR {filename}', f.write)
            downloaded_files.append(local_path)
            pbar.update(1)

    ftp.quit()
    return downloaded_files


def load_volume(file_path: str) -> np.ndarray:
    if file_path.endswith('.mrc'):
        import mrcfile
        with mrcfile.open(file_path, permissive=True) as mrc:
            return mrc.data.copy()
    elif file_path.endswith('.dm3'):
        dm3f = dm3.DM3(file_path)
        return dm3f.imagedata
    elif file_path.endswith('.ser'):
        data = ser.load_ser(file_path)
        return data['images'][0] if data['images'].shape[0] == 1 else data['images']
    else:
        raise ValueError(f"Unsupported file type: {file_path}")


def write_metadata_stub(
    entry_id: str,
    source_metadata: Dict[str, Any],
    file_path: str,
    volume_path: str,
    metadata_path: str,
    ftp_server: str
) -> Dict[str, Any]:
    # Initialize MetadataManager
    metadata_manager = MetadataManager()
    
    # Create standardized metadata record
    description = source_metadata.get("title", f"EBI EMPIAR dataset {entry_id}")
    record = metadata_manager.create_metadata_record(
        source="ebi",  # Fixed: was "empiar" 
        source_id=entry_id,
        description=description
    )
    
    # Add file paths
    metadata_manager.add_file_paths(
        record,
        volume_path=volume_path,
        raw_path=file_path,
        metadata_path=metadata_path
    )
    
    # Update status to saving-data
    metadata_manager.update_status(record, "saving-data")
    
    # Add download URL to provenance
    if "provenance" not in record["metadata"]:
        record["metadata"]["provenance"] = {}
    record["metadata"]["provenance"]["download_url"] = f"ftp://{ftp_server}/empiar/world_availability/{entry_id}/data/{os.path.basename(file_path)}"
    record["additional_metadata"] = source_metadata
    
    # Save stub metadata with validation
    metadata_manager.save_metadata(record, metadata_path, validate=False)  # Skip validation for stub
    return record


def enrich_metadata(
    metadata_path: str,
    record: Dict[str, Any],
    volume: np.ndarray
) -> None:
    # Initialize MetadataManager
    metadata_manager = MetadataManager()
    
    # Add technical metadata
    metadata_manager.add_technical_metadata(
        record,
        volume_shape=list(volume.shape),
        data_type="uint8",  # EBI data is typically uint8
        file_size_bytes=volume.nbytes,
        sha256=hashlib.sha256(volume.tobytes()).hexdigest()
    )
    
    # Update status to complete
    metadata_manager.update_status(record, "complete")
    
    # Save final metadata with validation
    metadata_manager.save_metadata(record, metadata_path, validate=True)


def process_empiar_file(
    entry_id: str,
    source_metadata: Dict[str, Any],
    file_path: str,
    output_dir: str,
    ftp_server: str
) -> str:
    try:
        volume = load_volume(file_path)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base_name = os.path.splitext(os.path.basename(file_path))[0]
        volume_path = os.path.join(output_dir, f"{base_name}_{timestamp}.npy")
        metadata_path = volume_path.replace(".npy", "_metadata.json")

        # Write stub metadata first
        stub = write_metadata_stub(entry_id, source_metadata, file_path, volume_path, metadata_path, ftp_server)

        # Save volume
        np.save(volume_path, volume)

        # Enrich metadata
        enrich_metadata(metadata_path, stub, volume)

        return f"Processed {file_path}"
    except Exception as e:
        return f"Failed {file_path}: {e}"


def ingest_empiar(config) -> None:
    entry_id = config.get('sources.ebi.defaults.entry_id', '11759')
    api_base_url = config.get('sources.ebi.base_urls.api', 'https://www.ebi.ac.uk/empiar/api/entry')
    ftp_server = config.get('sources.ebi.defaults.ftp_server', 'ftp.ebi.ac.uk')
    output_dir = os.environ.get('EM_DATA_DIR', config.get('sources.ebi.output_dir', './data/ebi'))
    max_workers = config.get('sources.ebi.defaults.max_workers', 4)
    
    # Ensure output directory exists
    os.makedirs(output_dir, exist_ok=True)
    
    metadata = fetch_metadata(entry_id, api_base_url)
    logger.info("Retrieved metadata for %s", entry_id)
    download_dir = os.path.join(output_dir, 'downloads')
    os.makedirs(download_dir, exist_ok=True)

    file_paths = download_files(entry_id, download_dir, ftp_server)
    logger.info("Downloaded %d files", len(file_paths))

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(process_empiar_file, entry_id, metadata, path, output_dir, ftp_server)
            for path in file_paths
        ]
        # Use tqdm with proper positioning to avoid jumbled output
        with tqdm(total=len(futures), desc="Processing volumes", 
                 bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]",
                 position=0, leave=True) as pbar:
            for future in as_completed(futures):
                result = future.result()
                if "Failed" in result:
                    logger.error(result)
                else:
                    logger.info(result)
                pbar.update(1)


def parse_args():
    parser = argparse.ArgumentParser(description='EBI EMPIAR data ingestion')
    parser.add_argument('--config', type=str, default=None,
                        help='Path to configuration file')
    parser.add_argument('--entry-id', type=str, default=None,
                        help='EMPIAR entry ID to process')
    return parser.parse_args()


def main():
    args = parse_args()
    
    # Initialize config manager
    config_manager = get_config_manager(args.config)
    
    # Override entry_id if provided via command line
    if args.entry_id:
        config_manager.set('sources.ebi.defaults.entry_id', args.entry_id)
    
    ingest_empiar(config_manager)


if __name__ == "__main__":
    main()
