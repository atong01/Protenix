# Copyright 2024 ByteDance and/or its affiliates.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import functools
import logging
import multiprocessing
import pickle
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional, Union

import biotite
import biotite.structure as struc
import biotite.structure.io.pdbx as pdbx
import gemmi
import numpy as np
import rdkit
import tqdm
from biotite.structure import AtomArray
from pdbeccdutils.core import ccd_reader
from rdkit import Chem

from configs.configs_data import data_configs
from protenix.data.substructure_perms import get_substructure_perms

logger = logging.getLogger(__name__)

COMPONENTS_FILE = data_configs["ccd_components_file"]
RKDIT_MOL_PKL = Path(data_configs["ccd_components_rdkit_mol_file"])


@functools.lru_cache
def biotite_load_ccd_cif() -> pdbx.CIFFile:
    """biotite load CCD components file

    Returns:
        pdbx.CIFFile: ccd components file
    """
    return pdbx.CIFFile.read(COMPONENTS_FILE)


@functools.lru_cache
def gemmi_load_ccd_cif() -> gemmi.cif.Document:
    """gemmi load CCD components file

    Returns:
        Document: gemmi ccd components file
    """
    return gemmi.cif.read(COMPONENTS_FILE)


def _map_central_to_leaving_groups(component) -> Optional[dict[str, list[list[str]]]]:
    """map each central atom (bonded atom) index to leaving atom groups in component (atom_array).

    Returns:
        dict[str, list[list[str]]]: central atom name to leaving atom groups (atom names).
    """
    comp = component.copy()
    # Eg: ions
    if comp.bonds is None:
        return {}
    central_to_leaving_groups = defaultdict(list)
    for c_idx in np.flatnonzero(~comp.leaving_atom_flag):
        bonds, _ = comp.bonds.get_bonds(c_idx)
        for l_idx in bonds:
            if comp.leaving_atom_flag[l_idx]:
                comp.bonds.remove_bond(c_idx, l_idx)
                group_idx = struc.find_connected(comp.bonds, l_idx)
                if not np.all(comp.leaving_atom_flag[group_idx]):
                    return None
                central_to_leaving_groups[comp.atom_name[c_idx]].append(
                    comp.atom_name[group_idx].tolist()
                )
    return central_to_leaving_groups


@functools.lru_cache
def get_component_atom_array(
    ccd_code: str, keep_leaving_atoms: bool = False, keep_hydrogens=False
) -> AtomArray:
    """get component atom array

    Args:
        ccd_code (str): ccd code
        keep_leaving_atoms (bool, optional): keep leaving atoms. Defaults to False.
        keep_hydrogens (bool, optional): keep hydrogens. Defaults to False.

    Returns:
        AtomArray: Biotite AtomArray of CCD component
            with additional attribute: leaving_atom_flag (bool)
    """
    ccd_cif = biotite_load_ccd_cif()
    if ccd_code not in ccd_cif:
        logger.warning(f"Warning: get_component_atom_array() can not parse {ccd_code}")
        return None
    try:
        comp = pdbx.get_component(ccd_cif, data_block=ccd_code, use_ideal_coord=True)
    except biotite.InvalidFileError as e:
        # Eg: UNL without atom.
        logger.warning(
            f"Warning: get_component_atom_array() can not parse {ccd_code} for {e}"
        )
        return None
    atom_category = ccd_cif[ccd_code]["chem_comp_atom"]
    leaving_atom_flag = atom_category["pdbx_leaving_atom_flag"].as_array()
    comp.set_annotation("leaving_atom_flag", leaving_atom_flag == "Y")

    for atom_id in ["alt_atom_id", "pdbx_component_atom_id"]:
        comp.set_annotation(atom_id, atom_category[atom_id].as_array())
    if not keep_leaving_atoms:
        comp = comp[~comp.leaving_atom_flag]
    if not keep_hydrogens:
        # EG: ND4
        comp = comp[~np.isin(comp.element, ["H", "D"])]

    # Map central atom index to leaving group (atom_indices) in component (atom_array).
    comp.central_to_leaving_groups = _map_central_to_leaving_groups(comp)
    if comp.central_to_leaving_groups is None:
        logger.warning(
            f"Warning: ccd {ccd_code} has leaving atom group bond to more than one central atom, central_to_leaving_groups is None."
        )
    return comp


@functools.lru_cache(maxsize=None)
def get_one_letter_code(ccd_code: str) -> Union[str, None]:
    """get one_letter_code from CCD components file.

    normal return is one letter: ALA --> A, DT --> T
    unknown protein: X
    unknown DNA or RNA: N
    other unknown: None
    some ccd_code will return more than one letter:
    eg: XXY --> THG

    Args:
        ccd_code (str): _description_

    Returns:
        str: one letter code
    """
    ccd_cif = biotite_load_ccd_cif()
    if ccd_code not in ccd_cif:
        return None
    one = ccd_cif[ccd_code]["chem_comp"]["one_letter_code"].as_item()
    if one == "?":
        return None
    else:
        return one


@functools.lru_cache(maxsize=None)
def get_mol_type(ccd_code: str) -> str:
    """get mol_type from CCD components file.

    based on _chem_comp.type
    http://mmcif.rcsb.org/dictionaries/mmcif_pdbx_v50.dic/Items/_chem_comp.type.html

    not use _chem_comp.pdbx_type, because it is not consistent with _chem_comp.type
    e.g. ccd 000 --> _chem_comp.type="NON-POLYMER" _chem_comp.pdbx_type="ATOMP"
    https://mmcif.wwpdb.org/dictionaries/mmcif_pdbx_v5_next.dic/Items/_struct_asym.pdbx_type.html

    Args:
        ccd_code (str): ccd code

    Returns:
        str: mol_type, one of {"protein", "rna", "dna", "ligand"}
    """
    ccd_cif = biotite_load_ccd_cif()
    if ccd_code not in ccd_cif:
        return "ligand"

    link_type = ccd_cif[ccd_code]["chem_comp"]["type"].as_item().upper()

    if "PEPTIDE" in link_type and link_type != "PEPTIDE-LIKE":
        return "protein"
    if "DNA" in link_type:
        return "dna"
    if "RNA" in link_type:
        return "rna"
    return "ligand"


def get_all_ccd_code() -> list:
    """get all ccd code from components file"""
    ccd_cif = biotite_load_ccd_cif()
    return list(ccd_cif.keys())


_ccd_rdkit_mols: dict[str, Chem.Mol] = {}


def get_component_rdkit_mol(ccd_code: str) -> Union[Chem.Mol, None]:
    """get rdkit mol by PDBeCCDUtils
    https://github.com/PDBeurope/ccdutils

    preprocessing all ccd components in _components_file at first time run.

    Args:
        ccd_code (str): ccd code

    Returns
        rdkit.Chem.Mol: rdkit mol with ref coord
    """
    global _ccd_rdkit_mols
    # _ccd_rdkit_mols is not empty
    if _ccd_rdkit_mols:
        return _ccd_rdkit_mols.get(ccd_code, None)

    rdkit_mol_pkl = RKDIT_MOL_PKL
    if rdkit_mol_pkl.exists():
        with open(rdkit_mol_pkl, "rb") as f:
            _ccd_rdkit_mols = pickle.load(f)
        return _ccd_rdkit_mols.get(ccd_code, None)

    # Preprocess all ccd components in _components_file at first time run.
    print("first time to run get_component_rdkit_mol().")
    print("preprocessing all ccd components in file:" f"{COMPONENTS_FILE}")
    print("pre-load cif file before multiprocessing avoid read file at each process.")
    gemmi_load_ccd_cif()

    mols = {}
    ccd_codes = get_all_ccd_code()
    cpu_count = multiprocessing.cpu_count() - 1
    with multiprocessing.Pool(cpu_count) as pool:
        for mol in tqdm.tqdm(
            pool.imap_unordered(_get_component_rdkit_mol_processing, ccd_codes),
            smoothing=0,
            total=len(ccd_codes),
        ):
            if mol is None:
                continue
            mols[mol.name] = mol
    # Success rate
    n_ccd = len(ccd_codes)
    print(f"success rate: {len(mols)/n_ccd:.2%} ({len(mols)}/{n_ccd})")

    # Sanitized rate
    sanitized_num = sum([mol.sanitized for mol in mols.values()])
    print(f"sanitized rate: {sanitized_num/n_ccd:.2%} ({sanitized_num}/{n_ccd})")

    # Rdkit conf rate
    rdkit_conf_num = sum([mol.ref_conf_type == "rdkit" for mol in mols.values()])
    print(f"rdkit conf rate: {rdkit_conf_num/n_ccd:.2%} ({rdkit_conf_num}/{n_ccd})")

    with open(rdkit_mol_pkl, "wb") as f:
        pickle.dump(mols, f)
    print(f"save rdkit mol to {rdkit_mol_pkl}")

    _ccd_rdkit_mols = mols
    return _ccd_rdkit_mols.get(ccd_code, None)


def _get_component_rdkit_mol_processing(ccd_code: str) -> Union[Chem.Mol, None]:
    """get rdkit mol by PDBeCCDUtils
    https://github.com/PDBeurope/ccdutils

    Args:
        ccd_code (str): ccd code

    Returns
        rdkit.Chem.Mol: rdkit mol with ref coord
    """
    ccd_cif = gemmi_load_ccd_cif()
    try:
        ccd_block = ccd_cif[ccd_code]
    except KeyError:
        return None
    ccd_reader_result = ccd_reader._parse_pdb_mmcif(ccd_block, sanitize=True)
    mol = ccd_reader_result.component.mol

    # Atom name from ccd, reading by pdbeccdutils
    # Copy atom name for pickle https://github.com/rdkit/rdkit/issues/2470
    mol.atom_map = {atom.GetProp("name"): atom.GetIdx() for atom in mol.GetAtoms()}

    mol.name = ccd_code
    mol.sanitized = ccd_reader_result.sanitized
    # First conf is ideal conf.
    mol.ref_conf_id = 0
    mol.ref_conf_type = "idea"

    num_atom = mol.GetNumAtoms()
    # Eg: UNL without atom
    if num_atom == 0:
        return mol

    # Make ref_mask, ref_mask is True if ideal coord is valid
    atoms = ccd_block.find(
        "_chem_comp_atom.", ["atom_id", "model_Cartn_x", "pdbx_model_Cartn_x_ideal"]
    )
    assert num_atom == len(atoms)
    ref_mask = np.zeros(num_atom, dtype=bool)
    for row in atoms:
        atom_id = gemmi.cif.as_string(row["_chem_comp_atom.atom_id"])
        atom_idx = mol.atom_map[atom_id]
        x_ideal = row["_chem_comp_atom.pdbx_model_Cartn_x_ideal"]
        ref_mask[atom_idx] = x_ideal != "?"
    mol.ref_mask = ref_mask

    if mol.sanitized == False:
        return mol
    options = rdkit.Chem.AllChem.ETKDGv3()
    options.clearConfs = False
    try:
        conf_id = rdkit.Chem.AllChem.EmbedMolecule(mol, options)
        mol.ref_conf_id = conf_id
        mol.ref_conf_type = "rdkit"
        mol.ref_mask[:] = True
    except ValueError:
        # Sanitization issue here
        logger.warning(f"Warning: fail to generate conf for {ccd_code}, use idea conf")
        pass
    return mol


@functools.lru_cache
def get_ccd_ref_info(ccd_code: str, return_perm: bool = True) -> dict[str, Any]:
    """
    Ref: AlphaFold3 SI Chapter 2.8
    Reference features. Features derived from a residue, nucleotide or ligand’s reference conformer.
    Given an input CCD code or SMILES string, the conformer is typically generated
    with RDKit v.2023_03_3 [25] using ETKDGv3 [26]. On error, we fall back to using the CCD ideal coordinates,
    or finally the representative coordinates
    if they are from before our training date cut-off (2021-09-30 unless otherwise stated).
    At the end, any atom coordinates still missing are set to zeros.

    Get reference atom mapping and coordinates.

    Args:
        name (str): CCD name
        return_perm (bool): return atom permutations.

    Returns:
        Dict:
            ccd: ccd code
            atom_map: atom name to atom index
            coord: atom coordinates
            charge: atom formal charge
            perm: atom permutation
    """
    mol = get_component_rdkit_mol(ccd_code)
    if mol is None:
        return {}
    if mol.GetNumAtoms() == 0:  # eg: "UNL"
        logger.warning(
            f"Warning: mol {ccd_code} from get_component_rdkit_mol() has no atoms,"
            "get_ccd_ref_info() return empty dict"
        )
        return {}
    conf = mol.GetConformer(mol.ref_conf_id)
    coord = conf.GetPositions()
    charge = np.array([atom.GetFormalCharge() for atom in mol.GetAtoms()])

    results = {
        "ccd": ccd_code,  # str
        "atom_map": mol.atom_map,  # dict[str,int]: atom name to atom index
        "coord": coord,  # np.ndarray[float]: atom coordinates, shape:(n_atom,3)
        "mask": mol.ref_mask,  # np.ndarray[bool]: atom mask, shape:(n_atom,)
        "charge": charge,  # np.ndarray[int]: atom formal charge, shape:(n_atom,)
    }

    if return_perm:
        try:
            Chem.SanitizeMol(mol)
            perm = get_substructure_perms(mol, MaxMatches=1000)

        except:
            # Sanitize failed, permutation is unavailable
            perm = np.array(
                [
                    [
                        i
                        for i, atom in enumerate(mol.GetAtoms())
                        if atom.GetAtomicNum() != 1
                    ]
                ]
            )
        # np.ndarray[int]: atom permutation, shape:(n_atom_wo_h, n_perm)
        results["perm"] = perm.T

    return results


# Modified from biotite to use consistent ccd components file
def _connect_inter_residue(
    atoms: AtomArray, residue_starts: np.ndarray
) -> struc.BondList:
    """
    Create a :class:`BondList` containing the bonds between adjacent
    amino acid or nucleotide residues.

    Parameters
    ----------
    atoms : AtomArray or AtomArrayStack
        The structure to create the :class:`BondList` for.
    residue_starts : ndarray, dtype=int
        Return value of
        ``get_residue_starts(atoms, add_exclusive_stop=True)``.

    Returns
    -------
    BondList
        A bond list containing all inter residue bonds.
    """

    bonds = []

    atom_names = atoms.atom_name
    res_names = atoms.res_name
    res_ids = atoms.res_id
    chain_ids = atoms.chain_id

    # Iterate over all starts excluding:
    #   - the last residue and
    #   - exclusive end index of 'atoms'
    for i in range(len(residue_starts) - 2):
        curr_start_i = residue_starts[i]
        next_start_i = residue_starts[i + 1]
        after_next_start_i = residue_starts[i + 2]

        # Check if the current and next residue is in the same chain
        if chain_ids[next_start_i] != chain_ids[curr_start_i]:
            continue
        # Check if the current and next residue
        # have consecutive residue IDs
        # (Same residue ID is also possible if insertion code is used)
        if res_ids[next_start_i] - res_ids[curr_start_i] > 1:
            continue

        # Get link type for this residue from RCSB components.cif
        curr_link = get_mol_type(res_names[curr_start_i])
        next_link = get_mol_type(res_names[next_start_i])

        if curr_link == "protein" and next_link in "protein":
            curr_connect_atom_name = "C"
            next_connect_atom_name = "N"
        elif curr_link in ["dna", "rna"] and next_link in ["dna", "rna"]:
            curr_connect_atom_name = "O3'"
            next_connect_atom_name = "P"
        else:
            # Create no bond if the connection types of consecutive
            # residues are not compatible
            continue

        # Index in atom array for atom name in current residue
        # Addition of 'curr_start_i' is necessary, as only a slice of
        # 'atom_names' is taken, beginning at 'curr_start_i'
        curr_connect_indices = np.where(
            atom_names[curr_start_i:next_start_i] == curr_connect_atom_name
        )[0]
        curr_connect_indices += curr_start_i

        # Index in atom array for atom name in next residue
        next_connect_indices = np.where(
            atom_names[next_start_i:after_next_start_i] == next_connect_atom_name
        )[0]
        next_connect_indices += next_start_i

        if len(curr_connect_indices) == 0 or len(next_connect_indices) == 0:
            # The connector atoms are not found in the adjacent residues
            # -> skip this bond
            continue

        bonds.append(
            (curr_connect_indices[0], next_connect_indices[0], struc.BondType.SINGLE)
        )

    return struc.BondList(atoms.array_length(), np.array(bonds, dtype=np.uint32))