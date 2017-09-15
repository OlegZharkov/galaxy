define(["libs/toastr","mvc/library/library-model","mvc/ui/ui-select"],function(a,b,c){var d=Backbone.View.extend({el:"#center",model:null,options:{},events:{"click .toolbtn_save_permissions":"savePermissions"},initialize:function(a){this.options=_.extend(this.options,a),this.options.id&&this.fetchFolder()},fetchFolder:function(c){this.options=_.extend(this.options,c),this.model=new b.FolderAsModel({id:this.options.id});var d=this;this.model.fetch({success:function(){d.options.show_permissions&&d.showPermissions()},error:function(b,c){"undefined"!=typeof c.responseJSON?a.error(c.responseJSON.err_msg+" Click this to go back.","",{onclick:function(){Galaxy.libraries.library_router.back()}}):a.error("An error occurred. Click this to go back.","",{onclick:function(){Galaxy.libraries.library_router.back()}})}})},showPermissions:function(b){this.options=_.extend(this.options,b),$(".tooltip").remove();var c=!1;Galaxy.user&&(c=Galaxy.user.isAdmin());var d=this.templateFolderPermissions();this.$el.html(d({folder:this.model,is_admin:c}));var e=this;$.get(Galaxy.root+"api/folders/"+e.id+"/permissions?scope=current").done(function(a){e.prepareSelectBoxes({fetched_permissions:a})}).fail(function(){a.error("An error occurred while attempting to fetch folder permissions.")}),$("#center [data-toggle]").tooltip(),$("#center").css("overflow","auto")},_serializeRoles:function(a){for(var b=[],c=0;c<a.length;c++)b.push(a[c][1]+":"+a[c][0]);return b},prepareSelectBoxes:function(a){this.options=_.extend(this.options,a);var b=this.options.fetched_permissions,d=this,e=this._serializeRoles(b.add_library_item_role_list),f=this._serializeRoles(b.manage_folder_role_list),g=this._serializeRoles(b.modify_folder_role_list);d.addSelectObject=new c.View(this._createSelectOptions(this,"add_perm",e,!1)),d.manageSelectObject=new c.View(this._createSelectOptions(this,"manage_perm",f,!1)),d.modifySelectObject=new c.View(this._createSelectOptions(this,"modify_perm",g,!1))},_createSelectOptions:function(a,b,c){var d={minimumInputLength:0,css:b,multiple:!0,placeholder:"Click to select a role",container:a.$el.find("#"+b),ajax:{url:Galaxy.root+"api/folders/"+a.id+"/permissions?scope=available",dataType:"json",quietMillis:100,data:function(a,b){return{q:a,page_limit:10,page:b}},results:function(a,b){var c=10*b<a.total;return{results:a.roles,more:c}}},formatResult:function(a){return a.name+" type: "+a.type},formatSelection:function(a){return a.name},initSelection:function(a,b){var c=[];$(a.val().split(",")).each(function(){var a=this.split(":");c.push({id:a[0],name:a[1]})}),b(c)},initialData:c.join(","),dropdownCssClass:"bigdrop"};return d},_extractIds:function(a){for(var b=[],c=a.length-1;c>=0;c--)b.push(a[c].id);return b},savePermissions:function(){var b=this,c=this._extractIds(this.addSelectObject.$el.select2("data")),d=this._extractIds(this.manageSelectObject.$el.select2("data")),e=this._extractIds(this.modifySelectObject.$el.select2("data"));$.post(Galaxy.root+"api/folders/"+b.id+"/permissions?action=set_permissions",{"add_ids[]":c,"manage_ids[]":d,"modify_ids[]":e}).done(function(c){b.showPermissions({fetched_permissions:c}),a.success("Permissions saved.")}).fail(function(){a.error("An error occurred while attempting to set folder permissions.")})},templateFolderPermissions:function(){return _.template(['<div class="library_style_container">','<div id="library_toolbar">','<a href="#/folders/<%= folder.get("parent_id") %>">','<button data-toggle="tooltip" data-placement="top" title="Go back to the parent folder" class="btn btn-default primary-button" type="button">','<span class="fa fa-caret-left fa-lg"/>',"&nbsp;Parent folder","</button>","</a>","</div>","<h1>",'Folder: <%= _.escape(folder.get("name")) %>',"</h1>",'<div class="alert alert-warning">',"<% if (is_admin) { %>","You are logged in as an <strong>administrator</strong> therefore you can manage any folder on this Galaxy instance. Please make sure you understand the consequences.","<% } else { %>","You can assign any number of roles to any of the following permission types. However please read carefully the implications of such actions.","<% }%>","</div>",'<div class="dataset_table">',"<h2>Folder permissions</h2>","<h4>","Roles that can manage permissions on this folder","</h4>",'<div id="manage_perm" class="manage_perm roles-selection"/>','<div class="alert alert-info roles-selection">',"User with <strong>any</strong> of these roles can manage permissions on this folder.","</div>","<h4>","Roles that can add items to this folder","</h4>",'<div id="add_perm" class="add_perm roles-selection"/>','<div class="alert alert-info roles-selection">',"User with <strong>any</strong> of these roles can add items to this folder (folders and datasets).","</div>","<h4>","Roles that can modify this folder","</h4>",'<div id="modify_perm" class="modify_perm roles-selection"/>','<div class="alert alert-info roles-selection">',"User with <strong>any</strong> of these roles can modify this folder (name, etc.).","</div>",'<button data-toggle="tooltip" data-placement="top" title="Save modifications" class="btn btn-default toolbtn_save_permissions primary-button" type="button">','<span class="fa fa-floppy-o"/>',"&nbsp;Save","</button>","</div>","</div>"].join(""))}});return{FolderView:d}});
//# sourceMappingURL=../../../maps/mvc/library/library-folder-view.js.map