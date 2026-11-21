import asyncio
import logging
import re
import tempfile
from pathlib import Path
from typing import (
     Any,
    Dict,
    List,
    Optional,
    Union,
)
from uuid import UUID

import requests
from psycopg2.extensions import connection as PgConnection

from apis.notion.file_upload import upload_local_file
from apis.notion.notional.blocks import (
    Block,
    Heading1,
    Heading2,
    Heading3,
    TextBlock,
    WithChildrenMixin,
)
from apis.notion.notional.text import TextObject
from apis.notion.notional.types import (
    MentionPage,
    Relation,
    UploadFile,
    URL,
)
from apis.notion.page import Page
from apis.notion.page_import import (
    append_and_check_block_l,
    upload_local_file_d,
)
from apis.notion.parser import HtmlParser
from apis.notion.session import get_session
from apis.notion.util import open_page_in_chrome
from apis.postgres.file_utils import get_absolute_path_from_local_path
from apis.postgres.query_executor import fetch_all

from src.db import (
    get_all_page_id_and_notion_id_from_db,
    get_child_page_l_with_notion_id,
    get_db_connection,
    get_notion_id_from_page_id,
    get_page_data_from_db,
    get_parent_page_l_with_notion_id,
    update_notion_id_in_db,
)
from src.webpage import (
    DEFAULT_WEBPAGE_DIR,
    NOTES_BASE_URL,
    get_page_id_from_url,
    get_url_from_page_id,
)


EVERGREEN_BOOK_ID = UUID("2ad024dc-88c4-8195-a7b8-dfdec7a6a8df")
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def get_all_block_l_recursively(
    block_l: List[Block],
) -> List[Block]:
    """
    Collect all blocks in `block_l` and any nested `children` blocks in depth-first order.
    """
    all_block_l: List[Block] = []
    for block in block_l:
        if isinstance(block, WithChildrenMixin):
            all_block_l.extend(block.get_flattened_block_l())
        else:
            all_block_l.append(block)
    return all_block_l


def mark_author_heading(
    heading: Block,
) -> Block:
    """
    Start the heading's title with the author heading marker `>`, as a heading of the source: [[Project/apis/doc/apis/notion/source-import]]
    """
    rich_text_l = heading("rich_text")
    if rich_text_l and isinstance(rich_text_l[0], TextObject):
        rich_text_l[0].text.content = ">" + rich_text_l[0].text.content
        rich_text_l[0].plain_text = ">" + rich_text_l[0].plain_text
    else:
        rich_text_l.insert(0, TextObject.build(">"))
    return heading


def lift_child_image(
    block: Block,
) -> List[Block]:
    """
    The block, then each image `HtmlParser` made its child from an `<img>` inside its text, so the image sits after its paragraph at the same level
    """
    if not isinstance(block, WithChildrenMixin) or not block("children"):
        return [block]
    image_l = [child for child in block("children") if child.type == "image"]
    if not image_l:
        return [block]
    rest_l = [child for child in block("children") if child.type != "image"]
    block().children = rest_l or None
    block.has_children = bool(rest_l)
    return [block, *image_l]


def get_notion_blocks_from_html(
    html_content: str,
    conn: Optional[PgConnection] = None,
    page_id_to_notion_id_d: Optional[Dict[str, Optional[str]]] = None,
    file_d: Optional[Dict[str, UploadFile]] = None,
) -> List[Block]:
    """
    Parse Evergreen Note HTML into Notion blocks with each image's upload from `file_d`, keyed by its `src`; drop the title heading, raise the other headings one level and mark them as author headings, and replace links to known Notion pages with `MentionPage`.
    """
    html_content = re.sub(
        r'href="/([a-zA-Z0-9_-]+)"',
        f'href="{NOTES_BASE_URL}/\\1"',
        html_content,
    )

    block_l = HtmlParser.parse_html_as_notion_block(html_content, file_d)
    block_l = [x for x in block_l if not isinstance(x, Heading1)]
    block_l = [y for x in block_l for y in lift_child_image(x)]

    for i, block in enumerate(block_l):
        if isinstance(block, Heading2):
            block_l[i] = mark_author_heading(Heading1.build(*block("rich_text")))
        elif isinstance(block, Heading3):
            block_l[i] = mark_author_heading(Heading2.build(*block("rich_text")))

    note_url_prefix = f"{NOTES_BASE_URL}/"
    for block in get_all_block_l_recursively(block_l):
        if isinstance(block, TextBlock):
            for j, text_block in enumerate(block("rich_text")):
                if text_block.href and text_block.href.startswith(note_url_prefix):
                    ref_page_id = get_page_id_from_url(text_block.href)
                    if page_id_to_notion_id_d is not None:
                        ref_notion_id = page_id_to_notion_id_d.get(ref_page_id)
                    else:
                        ref_notion_id = get_notion_id_from_page_id(ref_page_id, conn=conn)

                    if not ref_notion_id:
                        logging.info(f"Skipping href '{text_block.plain_text}'")
                    else:
                        block.rich_text_[j] = MentionPage.build(UUID(str(ref_notion_id)))
                        logging.info(f"Replacing href '{text_block.href}' with mention page '{ref_notion_id}'")

    return block_l


def get_image_src_l_from_html(
    html_content: str,
) -> List[str]:
    return re.findall(r'<img src="([^"]+)"', html_content)


async def upload_note_image_file_d(
    page_id: str,
    html_content: str,
) -> Dict[str, UploadFile]:
    """
    Upload each image of the saved note HTML `html_content` and map its `src` to the upload. The site moves its images, so each is downloaded from the same position in the note's live HTML; raises when the live note holds a different number of images, and the note must be crawled again.
    """
    saved_src_l = get_image_src_l_from_html(html_content)
    if not saved_src_l:
        return {}
    response = requests.get(get_url_from_page_id(page_id), headers={"User-Agent": USER_AGENT}, timeout=30)
    response.raise_for_status()
    live_src_l = get_image_src_l_from_html(response.text)
    assert len(live_src_l) == len(saved_src_l), f"Note {page_id} has {len(live_src_l)} images live and {len(saved_src_l)} saved: crawl it again"
    with tempfile.TemporaryDirectory() as dir_path:
        name_l = []
        for i, live_src in enumerate(live_src_l):
            image = requests.get(f"{NOTES_BASE_URL}/{live_src}", headers={"User-Agent": USER_AGENT}, timeout=60)
            image.raise_for_status()
            name = f"{i}-{live_src.rsplit('/', 1)[-1]}"
            Path(dir_path, name).write_bytes(image.content)
            name_l.append(name)
        upload_d = await upload_local_file_d(dir_path, name_l)
    return {saved_src: upload_d[name] for saved_src, name in zip(saved_src_l, name_l)}


async def append_and_report_block_l(
    notion_id: UUID,
    block_l: List[Block],
) -> None:
    """
    Append the note's blocks to its page, then print the page's block count and violations in local_notion
    """
    block_count, violation_l = await append_and_check_block_l(notion_id, block_l)
    print(f"  ✓ Appended the blocks: {block_count} blocks in local_notion")
    for violation in violation_l:
        print(f"  ✗ {violation.rule}{' (auto-fix)' if violation.is_auto_fix else ''}: {violation.message}")


async def create_all_notion_pages_and_update_db(
    filtered_note_d: Dict[str, Dict[str, Any]],
    conn: Optional[PgConnection] = None,
) -> None:
    """
    Create empty Notion pages in the `Chapter` database for all filtered notes and store their `notion_id` in PostgreSQL.
    """
    for page_id, page_info in filtered_note_d.items():
        existing_notion_id = get_notion_id_from_page_id(page_id, conn=conn)
        if existing_notion_id:
            continue

        title = page_info.get("name", "")
        url = page_info.get("url", "") or get_url_from_page_id(page_id)
        if not title:
            continue

        created_page_obj = await Page.create_in(
            title=title,
            data_source_name="Chapter",
            prop_d={"URL": URL.build(url)},
        )
        update_notion_id_in_db(page_id, created_page_obj.id, conn=conn)


async def upload_all_webpage_to_notion(
    webpage_dir: Union[str, Path] = DEFAULT_WEBPAGE_DIR,
) -> None:
    """
    Upload parsed Notion blocks for all pages in PostgreSQL that have a `notion_id` and no existing content.
    """
    conn = get_db_connection()
    try:
        page_with_notion_id_l = get_all_page_id_and_notion_id_from_db(conn=conn)
        for page_id, notion_id_str in page_with_notion_id_l:
            page_data_d = get_page_data_from_db(page_id, conn=conn)
            if not page_data_d or not page_data_d["file_location"]:
                continue

            file_path = Path(webpage_dir) / page_data_d["file_location"]
            if not file_path.exists():
                continue

            page_obj = await Page.retrieve(UUID(notion_id_str))
            if len(page_obj.content) > 1:
                continue

            html_content = file_path.read_text(encoding="utf-8")
            file_d = await upload_note_image_file_d(page_id, html_content)
            await append_and_report_block_l(page_obj.id, get_notion_blocks_from_html(html_content, conn=conn, file_d=file_d))
    finally:
        conn.close()


async def replace_href_with_mention_page_in_notion_page(
    page_id: Union[str, UUID],
    href_id: str,
    page_mention_id: Union[str, UUID],
) -> int:
    """
    Retrieve Notion page `page_id`, traverse all text blocks (including nested children), replace any link to `href_id` with a `MentionPage` pointing to `page_mention_id`, and return the number of updated blocks.
    """
    assert re.match(r"^[a-zA-Z0-9_-]+$", href_id)
    target_page_uuid = UUID(str(page_id)) if not isinstance(page_id, UUID) else page_id
    mention_uuid = UUID(str(page_mention_id)) if not isinstance(page_mention_id, UUID) else page_mention_id
    target_url = get_url_from_page_id(href_id)

    page_obj = await Page.retrieve(target_page_uuid)
    block_l = get_all_block_l_recursively(page_obj.content)

    updated_block_count = 0
    for block in block_l:
        if not isinstance(block, TextBlock):
            continue

        block_modified = False
        for j, text_block in enumerate(block("rich_text")):
            if text_block.href and text_block.href == target_url:
                block.rich_text_[j] = MentionPage.build(mention_uuid)
                block_modified = True

        if block_modified:
            await get_session().blocks.update(block)
            updated_block_count += 1

    return updated_block_count


async def upload_new_webpage_to_notion(
    url_or_page_id: str,
    webpage_dir: Union[str, Path] = DEFAULT_WEBPAGE_DIR,
    confirm_template: bool = True,
    do_open_url: bool = True,
) -> UUID:
    """
    Upload a new Evergreen Note page to Notion `Chapter` database, update `notion_id` in PostgreSQL, populate `📖 Related`, and replace links to this page with `MentionPage` across all parent Notion pages.
    """
    page_id = get_page_id_from_url(url_or_page_id)
    conn = get_db_connection()

    try:
        print(f"\nProcessing page: {page_id}")

        page_data_d = get_page_data_from_db(page_id, conn=conn)
        if not page_data_d:
            raise ValueError(f"Page {page_id} not found in database")

        title = page_data_d["title"]
        file_location = page_data_d["file_location"]
        existing_notion_id = page_data_d["notion_id"]

        if existing_notion_id:
            raise ValueError(f"Page {page_id} already has notion_id: {existing_notion_id}. This function is for new pages only.")
        if not file_location:
            raise ValueError(f"Page {page_id} has no file_location in database")

        file_path = Path(file_location)
        if not file_path.is_absolute():
            file_path = Path(webpage_dir) / file_path
        if not file_path.exists():
            raise FileNotFoundError(f"HTML file not found: {file_path}")

        print(f"  Title: {title}")
        print(f"  File location: {file_location}")
        print("  Creating Notion page...")

        url = get_url_from_page_id(page_id)
        leaf_icon_row_l = fetch_all("SELECT local_path FROM metadata.file WHERE id = %s AND type = 'icon'", (EVERGREEN_BOOK_ID,)) # the Evergreen book page's own leaf icon
        icon = await upload_local_file(get_absolute_path_from_local_path(leaf_icon_row_l[0][0]), file_name_with_ext="leaf.png") if leaf_icon_row_l else None
        created_page_obj = await Page.create_in(
            title=title,
            data_source_name="Chapter",
            rel_d={"📚 Book": EVERGREEN_BOOK_ID},
            prop_d={"URL": URL.build(url)},
            icon=icon,
        )
        if do_open_url:
            open_page_in_chrome(created_page_obj)

        notion_id = created_page_obj.id
        update_notion_id_in_db(page_id, notion_id, conn=conn)
        print(f"  ✓ Created Notion page: {notion_id}")

        if confirm_template:
            confirm = input("Initialized the page with the evergreen chapter template? (y/N): ").strip().lower()
            while confirm != "y":
                confirm = input("Initialized the page with the evergreen chapter template? (y/N): ").strip().lower()

        html_content = file_path.read_text(encoding="utf-8")
        file_d = await upload_note_image_file_d(page_id, html_content)
        block_l = get_notion_blocks_from_html(html_content, conn=conn, file_d=file_d)

        child_page_l = get_child_page_l_with_notion_id(page_id, conn=conn)
        parent_page_l = get_parent_page_l_with_notion_id(page_id, conn=conn)
        child_notion_id_l = [UUID(nid) for _, _, nid in child_page_l]
        parent_notion_id_l = [UUID(nid) for _, _, nid in parent_page_l]
        unique_related_id_l = list(dict.fromkeys(child_notion_id_l + parent_notion_id_l))

        if unique_related_id_l:
            created_page_obj.stage({"📖 Related": Relation.build(*unique_related_id_l)})
            print(f'  Updating the "📖 Related" property of the new page with {len(unique_related_id_l)} related pages...')

        await created_page_obj.commit()
        print(f"  Appending {len(block_l)} blocks after the template's...")
        await append_and_report_block_l(notion_id, block_l)

        if parent_notion_id_l:
            print("  Parent pages:")
            for parent_notion_id in parent_notion_id_l:
                parent_url = f"https://notion.so/{str(parent_notion_id).replace('-', '')}"
                print(f"    - {parent_url}")
                await replace_href_with_mention_page_in_notion_page(
                    parent_notion_id,
                    page_id,
                    notion_id,
                )

        print(f"  ✓ Successfully processed page {page_id}")
        return notion_id

    except Exception as e:
        print(f"  ✗ Error processing page {page_id}: {e}")
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    async def main() -> None:
        page_url_l = [
            "https://notes.andymatuschak.org/z9vSQjkBVL6dCVC6QhCu4Br",
        ]
        for url in page_url_l:
            await upload_new_webpage_to_notion(url)

    asyncio.run(main())
