from apibara.starknet import EventFilter, Filter, StarkNetIndexer, felt
from starknet_py.contract import ContractFunction
from apibara.indexer import Info
from apibara.starknet.cursor import starknet_cursor
from apibara.protocol.proto.stream_pb2 import Cursor, DataFinality
from apibara.indexer.indexer import IndexerConfiguration
from apibara.starknet.proto.starknet_pb2 import Block
from apibara.starknet.proto.types_pb2 import FieldElement
from typing import List


def decode_felt_to_domain_string(felt):
    def extract_stars(str):
        k = 0
        while str.endswith(bigAlphabet[-1]):
            str = str[:-1]
            k += 1
        return (str, k)

    basicAlphabet = "abcdefghijklmnopqrstuvwxyz0123456789-"
    bigAlphabet = "这来"

    decoded = ""
    while felt != 0:
        code = felt % (len(basicAlphabet) + 1)
        felt = felt // (len(basicAlphabet) + 1)
        if code == len(basicAlphabet):
            next_felt = felt // (len(bigAlphabet) + 1)
            if next_felt == 0:
                code2 = felt % (len(bigAlphabet) + 1)
                felt = next_felt
                decoded += basicAlphabet[0] if code2 == 0 else bigAlphabet[code2 - 1]
            else:
                decoded += bigAlphabet[felt % len(bigAlphabet)]
                felt = felt // len(bigAlphabet)
        else:
            decoded += basicAlphabet[code]

    decoded, k = extract_stars(decoded)
    if k:
        decoded += (
            ((bigAlphabet[-1] * (k // 2 - 1)) + bigAlphabet[0] + basicAlphabet[1])
            if k % 2 == 0
            else bigAlphabet[-1] * (k // 2 + 1)
        )

    return decoded


class Listener(StarkNetIndexer):
    def __init__(self, conf) -> None:
        super().__init__()
        self.conf = conf
        self.handle_pending_data = self.handle_data
        self._last_block_number = None

    def check_is_subdomain(self, contract: FieldElement):
        if felt.to_int(contract) == int(self.conf.braavos_contract, 16):
            return (True, "braavos")
        elif felt.to_int(contract) == int(self.conf.xplorer_contract, 16):
            return (True, "xplorer")
        else:
            return (False, "")
        
    def on_block(self, block: Block):
        self._last_block_number = block.header.block_number
        
    @property
    def last_block_number(self):
        return self._last_block_number

    def indexer_id(self) -> str:
        return self.conf.indexer_id

    def initial_configuration(self) -> Filter:
        filter = Filter().with_header(weak=True)
        self.event_map = dict()

        def add_filter(contract, event):
            selector = ContractFunction.get_selector(event)
            self.event_map[selector] = event
            filter.add_event(
                EventFilter()
                .with_from_address(felt.from_hex(contract))
                .with_keys([felt.from_int(selector)])
            )

        # starknet_id contract
        for starknet_id_event in [
            "Transfer",
        ]:
            add_filter(self.conf.starknetid_contract, starknet_id_event)

        # naming contract
        for starknet_id_event in [
            "domain_to_addr_update",
            "addr_to_domain_update",
            "starknet_id_update",
            "domain_transfer",
        ]:
            add_filter(self.conf.naming_contract, starknet_id_event)

        # auto renewal contract
        for starknet_id_event in [
            "toggled_renewal",
        ]:
            add_filter(self.conf.renewal_contract, starknet_id_event)

        # erc20 contract
        for starknet_id_event in [
            "Approval",
        ]:
            add_filter(self.conf.erc20_contract, starknet_id_event)

        return IndexerConfiguration(
            filter=filter,
            starting_cursor=starknet_cursor(self.conf.starting_block),
            finality=DataFinality.DATA_STATUS_ACCEPTED if self.conf.is_devnet is True else DataFinality.DATA_STATUS_PENDING,
        )

    async def handle_data(self, info: Info, block: Block):
        self.on_block(block)
        # Handle one block of data
        for event_with_tx in block.events:
            tx_hash = felt.to_hex(event_with_tx.transaction.meta.hash)
            event = event_with_tx.event
            event_name = self.event_map[felt.to_int(event.keys[0])]

            await {
                "Transfer": self.on_starknet_id_transfer,
                "domain_to_addr_update": self.domain_to_addr_update,
                "addr_to_domain_update": self.addr_to_domain_update,
                "starknet_id_update": self.starknet_id_update,
                "domain_transfer": self.domain_transfer,
                "toggled_renewal": self.renewal_on_toggled_renewal,
                "Approval": self.renewal_on_approval,
            }[event_name](info, block, event.from_address, event.data)

    async def on_starknet_id_transfer(
        self, info: Info, block: Block, _: FieldElement, data: List[FieldElement]
    ):
        source = str(felt.to_int(data[0]))
        target = str(felt.to_int(data[1]))
        token_id = str(felt.to_int(data[2]) + (felt.to_int(data[3]) << 128))
        # update existing owner
        existing = False
        if source != "0":
            existing = await info.storage.find_one_and_update(
                "starknet_ids",
                {"token_id": token_id, "_chain.valid_to": None},
                {"$set": {"owner": target}},
            )
        if not existing:
            await info.storage.insert_one(
                "starknet_ids",
                {
                    "owner": target,
                    "token_id": token_id,
                    "creation_date": block.header.timestamp.ToDatetime(),
                },
            )

        print("- [transfer]", token_id, source, "->", target)

    async def domain_to_addr_update(
        self, info: Info, block: Block, contract: FieldElement, data: List[FieldElement]
    ):
        (is_subdomain, project) = self.check_is_subdomain(contract)
        arr_len = felt.to_int(data[0])
        domain = ""
        for i in range(arr_len):
            domain += decode_felt_to_domain_string(felt.to_int(data[1 + i])) + "."
        if domain:
            if is_subdomain:
                domain += project + "."
            domain += "stark"
        address = data[arr_len + 1]

        if domain:
            if is_subdomain:
                await info.storage.insert_one(
                    "subdomains",
                    {
                        "domain": domain,
                        "project": project,
                        "creation_date": block.header.timestamp.ToDatetime(),
                        "addr": str(felt.to_int(address)),
                    },
                )
            else:
                await info.storage.find_one_and_update(
                    "domains",
                    {"domain": domain, "_chain.valid_to": None},
                    {"$set": {"addr": str(felt.to_int(address))}},
                )
        else:
            if is_subdomain:
                await info.storage.find_one_and_update(
                    "subdomains",
                    {"domain": domain, "project": project, "_chain.valid_to": None},
                    {"$unset": {"addr": None}},
                )
            else:
                await info.storage.find_one_and_update(
                    "domains",
                    {"domain": domain, "_chain.valid_to": None},
                    {"$unset": {"addr": None}},
                )
        print("- [domain2addr]", domain, "->", felt.to_hex(address))

    async def addr_to_domain_update(
        self, info: Info, block: Block, contract: FieldElement, data: List[FieldElement]
    ):
        address = data[0]
        arr_len = felt.to_int(data[1])
        domain = ""
        for i in range(arr_len):
            domain += decode_felt_to_domain_string(felt.to_int(data[2 + i])) + "."
        if domain:
            domain += "stark"

        str_address = str(felt.to_int(address))

        await info.storage.find_one_and_update(
            "domains",
            {"rev_addr": str_address, "_chain.valid_to": None},
            {"$unset": {"rev_addr": None}},
        )
        if domain:
            if domain.endswith(".braavos.stark") or domain.endswith(".xplorer.stark"):
                await info.storage.find_one_and_update(
                    "subdomains",
                    {"domain": domain, "_chain.valid_to": None},
                    {"$set": {"rev_addr": str_address}},
                )
            else:
                await info.storage.find_one_and_update(
                    "domains",
                    {"domain": domain, "_chain.valid_to": None},
                    {"$set": {"rev_addr": str_address}},
                )
        print("- [addr2domain]", felt.to_hex(address), "->", domain)

    async def starknet_id_update(
        self, info: Info, block: Block, contract: FieldElement, data: List[FieldElement]
    ):
        arr_len = felt.to_int(data[0])
        domain = ""
        for i in range(arr_len):
            domain += decode_felt_to_domain_string(felt.to_int(data[1 + i])) + "."
        if domain:
            domain += "stark"
        owner = str(felt.to_int(data[arr_len + 1]))
        expiry = felt.to_int(data[arr_len + 2])

        # we want to upsert
        existing = await info.storage.find_one_and_update(
            "domains",
            {"domain": domain, "_chain.valid_to": None},
            {
                "$set": {
                    "domain": domain,
                    "expiry": expiry,
                    "token_id": owner,
                }
            },
        )
        if existing is None:
            await info.storage.insert_one(
                "domains",
                {
                    "domain": domain,
                    "expiry": expiry,
                    "token_id": owner,
                    "creation_date": block.header.timestamp.ToDatetime(),
                },
            )
            print(
                "- [purchased]",
                "domain:",
                domain,
                "id:",
                owner,
            )
        else:
            await info.storage.insert_one(
                "domains_renewals",
                {
                    "domain": domain,
                    "prev_expiry": existing["expiry"],
                    "new_expiry": expiry,
                    "renewal_date": block.header.timestamp.ToDatetime(),
                },
            )
            print(
                "- [renewed]",
                "domain:",
                domain,
                "id:",
                owner,
                "time:",
                (expiry - int(existing["expiry"])) / 86400,
                "days",
            )

    async def domain_transfer(
        self, info: Info, block: Block, contract: FieldElement, data: List[FieldElement]
    ):
        arr_len = felt.to_int(data[0])
        domain = ""
        for i in range(arr_len):
            domain += decode_felt_to_domain_string(felt.to_int(data[1 + i])) + "."
        if domain:
            domain += "stark"
        prev_owner = str(felt.to_int(data[arr_len + 1]))
        new_owner = str(felt.to_int(data[arr_len + 2]))

        if prev_owner != "0":
            await info.storage.find_one_and_update(
                "domains",
                {
                    "domain": domain,
                    "token_id": prev_owner,
                    "_chain.valid_to": None,
                },
                {"$set": {"token_id": new_owner}},
            )
        else:
            await info.storage.insert_one(
                "domains",
                {
                    "domain": domain,
                    "addr": "0",
                    "expiry": None,
                    "token_id": new_owner,
                    "creation_date": block.header.timestamp.ToDatetime(),
                },
            )

        print(
            "- [domain_transfer]",
            domain,
            prev_owner,
            "->",
            new_owner,
        )


    async def renewal_on_toggled_renewal(
        self, info: Info, block: Block, _: FieldElement, data: List[FieldElement]
    ):
        domain = decode_felt_to_domain_string(felt.to_int(data[0]))
        renewer_addr = str(felt.to_int(data[1]))
        value = felt.to_int(data[2]) 

        existing = False
        existing = await info.storage.find_one_and_update(
            "auto_renewals",
            {"domain": domain, "renewer_address": renewer_addr, "_chain.valid_to": None},
            {"$set": {"auto_renewal_enabled": value}},
        )

        if not existing:
            await info.storage.insert_one(
                "auto_renewals",
                {
                    "domain": domain,
                    "renewer_address": renewer_addr,
                    "auto_renewal_enabled": value,
                },
            )
        print(
            "- [on_toggled_renewal]",
            "renewer:",
            renewer_addr,
            "domain:",
            domain,
            "auto_renewal_enabled:",
            value,
            "timestamp:",
            block.header.timestamp.ToDatetime(),
        )

    async def renewal_on_approval(
        self, info: Info, block: Block, _: FieldElement, data: List[FieldElement]
    ):
        renewal_contract = self.conf.renewal_contract
        renewer = str(felt.to_int(data[0]))
        spender = felt.to_hex(data[1])
        allowance = str(felt.to_int(data[2]))

        existing = False
        if spender == renewal_contract:
            existing = await info.storage.find_one_and_update(
                "approvals",
                {"renewer": renewer, "_chain.valid_to": None},
                {"$set": {"value": allowance}},
            )

            if not existing:
                await info.storage.insert_one(
                    "approvals",
                    {
                        "renewer": renewer,
                        "value": allowance,
                    },
                )
            print(
                "- [on_approval]",
                "renewer:",
                renewer,
                "value:",
                allowance,
                "timestamp:",
                block.header.timestamp.ToDatetime(),
            )